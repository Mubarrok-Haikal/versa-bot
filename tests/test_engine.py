import gc
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

import requests

import engine
from runtime import AimClock, PrecisionWindow


def job(**values):
    return dict(cmid=10, course_id=2, name='Test', course='Test course',
                at=datetime.now(engine.WIB), files=[], online_text='My own assignment', finalize=True, **values)


class FakeBot:
    """Moodle stub with configurable response latency."""
    username = password = 'test'
    closed_notices = set()
    ws_functions = {'mod_assign_get_submission_status'}
    rtt = 1.1
    save_reply = engine.Hebat.save_reply

    def __init__(self, latency=0.05, mode='normal'):
        self.latency, self.mode = latency, mode
        self.go = 0
        self.calls = []
        self.state = 'new'
        self.status_reads = self.active = self.max_active = 0
        self.lock = threading.Lock()
        self.submit_seen = threading.Event()

    def send_ws(self, request):
        with self.lock:
            self.calls.append((request, time.time() - self.go))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if request == 'submit':
                self.submit_seen.set()
                self.state = 'submitted'
                return []
            if self.mode == 'fatal':
                return [{'warningcode': 'couldnotsavesubmission', 'item': 'Wrong file type'}]
            if self.mode in ('timeout_saved', 'timeout_empty'):
                if self.mode == 'timeout_saved':
                    self.state = 'submitted'
                time.sleep(self.latency)
                raise requests.ReadTimeout('lost response')
            now = time.time()
            took = now >= self.go
            if took:
                self.state = 'draft'
            time.sleep(self.latency)
            return [] if took else [{'warningcode': 'submissionnotopen', 'message': 'Not open'}]
        finally:
            with self.lock:
                self.active -= 1

    def ws_status(self, assign_id, team=False):
        self.status_reads += 1
        return {'state': self.state, 'modified': 2 if self.state != 'new' else 0}


def runner(bot, delta=0.4, drafts=True):
    r = engine.Runner(bot, job(), Mock())
    r.go = bot.go = time.time() + delta
    r.aim = AimClock(r.go)
    r.ws_info = {'id': 7, 'drafts': drafts, 'rules': {'timelimit': 0}}
    r.before_status = {'state': 'new', 'modified': 0}
    r.save_request, r.submit_request = 'save', 'submit'
    r.ws_limit = 16
    r.warm = Mock()
    return r


class SubmissionTests(unittest.TestCase):
    def test_ready_form_does_not_add_a_status_round_trip(self):
        b = FakeBot()
        r = runner(b, delta=-0.1)
        r.ws_info = None
        form = engine.find_form_fast('<form><input name="action" value="savesubmission"></form>', 'savesubmission')
        b.edit_form = Mock(return_value=(form, Mock(text='form html')))
        r.status = Mock(side_effect=AssertionError('unnecessary network request'))
        self.assertEqual(r.wait_for_form(False, None, True), (b, form, 'form html'))

    def test_full_run_confirms_saved_and_submitted(self):
        b = FakeBot()
        r = runner(b, delta=-0.1)
        r.setup_ws = r.check_clock = r.preflight = r.prepare_requests = Mock()
        r.early_upload = Mock(return_value=None)
        r.work()
        self.assertEqual(r.state, 'done')
        self.assertIn('submitted for grading', r.result)
        self.assertTrue(b.submit_seen.is_set())

    def test_website_winner_submits_before_draining_other_checks(self):
        b = FakeBot()
        r = runner(b, delta=0.4)
        r.ws_info = None
        r.submit_request = None
        b.blind_save = Mock(side_effect=lambda *args: (time.sleep(0.15) or True, None, None))
        b.submit_for_grading = Mock(return_value=True)
        r.wait_for_form(True, None, True)
        b.submit_for_grading.assert_called_once_with(10)
        self.assertTrue(r.submit_ack.is_set())

    def test_slow_no_drafts_cutoff_drains_and_reserves_opening_slots(self):
        b = FakeBot(latency=1.1)
        r = runner(b, drafts=False)
        r.wait_for_form(True, None, True)
        saves = [t for kind, t in b.calls if kind == 'save']
        self.assertTrue(any(0 <= t <= engine.SAVE_CUTOFF for t in saves), saves)
        self.assertLessEqual(max(saves), engine.SAVE_CUTOFF + 0.01)
        self.assertLessEqual(b.max_active, 16)
        self.assertEqual(b.active, 0)
        self.assertFalse(b.submit_seen.is_set())
        self.assertTrue(r.saved_ack.is_set())

    def test_accepting_worker_submits_before_result_and_progress_log(self):
        b = FakeBot()
        r = runner(b, delta=-0.1)
        def log(_):
            self.assertTrue(b.submit_seen.is_set())
        r.log = log
        r.wait_for_form(True, None, True)
        self.assertTrue(r.submit_ack.is_set())
        self.assertEqual([k for k, _ in b.calls], ['save', 'submit'])

    def test_timeout_reconciles_accepted_save_without_resending(self):
        b = FakeBot(mode='timeout_saved')
        r = runner(b, delta=-0.1, drafts=False)
        r.wait_for_form(True, None, True)
        self.assertEqual(len(b.calls), 1)
        self.assertEqual(b.status_reads, 1)
        self.assertTrue(r.saved_ack.is_set())

    def test_timeout_not_mistaken_for_closed_or_retried(self):
        b = FakeBot(mode='timeout_empty')
        r = runner(b, delta=-0.1)
        with self.assertRaisesRegex(engine.HebatError, 'lost or blocked'):
            r.wait_for_form(True, None, True)
        self.assertEqual(len(b.calls), 1)
        self.assertEqual(b.status_reads, 1)

    def test_validation_refusal_is_fatal_and_gets_final_status(self):
        b = FakeBot(mode='fatal')
        r = runner(b)
        with self.assertRaisesRegex(engine.WsError, 'Wrong file type'):
            r.wait_for_form(True, None, True)
        self.assertEqual(b.status_reads, 1)

    def test_preexisting_submission_not_reported_as_new_success(self):
        b = FakeBot()
        r = runner(b)
        r.before_status = {'state': 'submitted', 'modified': 2}
        b.state = 'submitted'
        self.assertFalse(r.recovered_save())

    def test_retry_only_after_all_refusals(self):
        b = FakeBot(latency=0.01)
        r = runner(b, delta=-0.2)
        original = b.send_ws
        count = 0
        def save(req):
            nonlocal count
            if req == 'save':
                count += 1
                if count == 1:
                    b.calls.append((req, time.time() - b.go))
                    return [{'warningcode': 'submissionnotopen'}]
            return original(req)
        b.send_ws = save
        r.wait_for_form(True, None, True)
        times = [t for kind, t in b.calls if kind == 'save']
        self.assertEqual(len(times), 2)
        self.assertGreaterEqual(times[1] - times[0], engine.FAST_POLL - 0.01)

    def test_stop_drains_and_checks_inflight_save(self):
        b = FakeBot(latency=0.15, mode='timeout_saved')
        r = runner(b, delta=-0.1)
        stopper = threading.Timer(0.03, r.stop)
        stopper.start()
        try:
            r.wait_for_form(True, None, True)
        finally:
            stopper.join()
        self.assertEqual(b.active, 0)
        self.assertEqual(b.status_reads, 1)
        self.assertTrue(r.saved_ack.is_set())


class APIAndPreflightTests(unittest.TestCase):
    def setUp(self):
        self.b = engine.Hebat('test', 'test')
        self.addCleanup(self.b.s.close)
        self.addCleanup(self.b.ws_s.close)

    def test_failed_login_preserves_session_cookie_and_identity(self):
        self.b.s.cookies.set('MoodleSession', 'old')
        self.b.sesskey, self.b.user_id = 'old-key', 5
        old = self.b.s
        candidate = Mock()
        candidate.get.side_effect = requests.Timeout()
        with patch('engine.new_session', return_value=candidate), self.assertRaises(requests.Timeout):
            self.b.login()
        self.assertIs(self.b.s, old)
        self.assertEqual(self.b.s.cookies.get('MoodleSession'), 'old')
        self.assertEqual((self.b.sesskey, self.b.user_id), ('old-key', 5))
        candidate.close.assert_called_once()

    def test_warning_classification_uses_sent_time_and_notice(self):
        self.b.closed_notices = {'The due date for this assignment has now passed'}
        warning = [{'warningcode': 'couldnotsavesubmission', 'item': next(iter(self.b.closed_notices))}]
        self.assertFalse(self.b.save_reply(warning, closed_until=100, checked_at=99))
        with self.assertRaises(engine.WsError):
            self.b.save_reply(warning, closed_until=100, checked_at=101)
        with self.assertRaises(engine.WsError):
            self.b.save_reply([{'warningcode': 'couldnotsavesubmission', 'item': 'Invalid file'}], 100, 99)
        with self.assertRaises(engine.WsError):
            self.b.save_reply([warning[0], {'warningcode': 'badfile'}], 100, 99)

    def test_login_error_never_becomes_closed(self):
        response = Mock(is_redirect=False)
        response.json.return_value = {'exception': 'require_login_exception', 'errorcode': 'requireloginerror', 'message': 'Login required'}
        self.b.ws_s.send = Mock(return_value=response)
        with self.assertRaises(engine.WsError):
            self.b.send_ws(Mock())

    def test_unrecognized_website_status_is_not_closed_forever(self):
        response = requests.Response()
        response.status_code = 200
        response.url = engine.BASE + '/mod/assign/view.php'
        response._content = b'<table class="submissionstatustable"><td class="submissionstatusnew">Nothing saved</td></table>'
        with self.assertRaises(engine.HebatError):
            self.b.check_web_refusal(response)
        self.b.closed_until = time.time() + 60
        self.b.check_web_refusal(response)

    def test_http_403_429_and_challenge_are_outages(self):
        for status, text in [(403, ''), (429, ''), (503, ''), (200, '<title>Just a moment</title>')]:
            response = requests.Response()
            response.status_code = status
            response.headers['Content-Type'] = 'text/html'
            response._content = text.encode()
            with self.subTest(status=status), self.assertRaises(requests.HTTPError):
                engine.overloaded(response)

    def preflight_runner(self, path, **config):
        r = engine.Runner(self.b, job(), Mock())
        r.job.update(files=[path], online_text='')
        configs = {'enabled': '1', 'maxfilesubmissions': '1', 'maxsubmissionsizebytes': '100', 'filetypeslist': '.pdf'}
        configs.update(config)
        r.ws_info = {'id': 7, 'rules': {'timelimit': 0, 'configs': [
            {'plugin': 'file', 'subtype': 'assignsubmission', 'name': key, 'value': value}
            for key, value in configs.items()]}}
        return r

    def test_file_rules_and_timed_assignment_fail_before_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'work.pdf'
            path.write_bytes(b'x' * 10)
            r = self.preflight_runner(path)
            r.preflight()
            with self.assertRaisesRegex(engine.HebatError, 'upload limit'):
                self.preflight_runner(path, maxsubmissionsizebytes='5').preflight()
            with self.assertRaisesRegex(engine.HebatError, 'file type'):
                self.preflight_runner(path, filetypeslist='.docx').preflight()
            with self.assertRaisesRegex(engine.HebatError, 'allows 0'):
                self.preflight_runner(path, maxfilesubmissions='0').preflight()
            r.ws_info['rules']['timelimit'] = 600
            with self.assertRaisesRegex(engine.HebatError, 'timed assignment'):
                r.preflight()

    def test_moodle_group_filetypes_expand_through_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'work.pdf'
            path.write_bytes(b'x')
            r = self.preflight_runner(path, filetypeslist='document')
            self.b.ajax = Mock(return_value={'groups': [{'key': 'document', 'ext': '.pdf', 'types': [{'key': '.pdf', 'ext': 'pdf'}]}]})
            r.preflight()
            self.b.ajax.assert_called_once()

    def test_php_clock_preferred_over_date(self):
        self.b.token = 'test'
        self.b.ws_functions = {'core_calendar_get_calendar_upcoming_view'}
        self.b.ws = Mock(return_value={'date': {'timestamp': 1234}})
        def measure(read):
            self.assertEqual(read(), 1234)
            return 0.125
        self.b.measure_clock = measure
        with patch('engine.new_session', side_effect=AssertionError('must not use Date')):
            self.assertEqual(self.b.clock_offset(), 0.125)
        self.assertEqual(self.b.clock_source, 'Moodle/PHP')

    def test_ca_store_cached_and_proxy_discovery_not_per_send(self):
        with engine.new_session() as other:
            self.assertIs(self.b.s.get_adapter('https://').context, other.get_adapter('https://').context)
            self.assertFalse(other.trust_env)


class FormAndRuntimeTests(unittest.TestCase):
    def test_paused_console_cannot_block_log(self):
        import queue
        import versa_bot
        app = versa_bot.App.__new__(versa_bot.App)
        app.seq, app.logs, app.lock = 0, [], threading.Lock()
        app.console = queue.Queue(maxsize=1)
        app.console.put_nowait('console is backed up')
        with patch('builtins.print', side_effect=AssertionError('inline print is forbidden')):
            app.log('Still submitting')
        self.assertEqual(app.logs[0]['msg'], 'Still submitting')

    def test_attribute_can_contain_greater_than_sign(self):
        form = engine.find_form_fast('<form><input name="action" value="savesubmission"><input name="x" value="a > b"></form>', 'savesubmission')
        self.assertEqual(engine.form_data(form)['x'], 'a > b')

    def test_fast_form_handles_quotes_entities_controls_and_other_forms(self):
        text = """<form><input name='action' value='other'></form><FORM id='mform'>
          <input value='savesubmission' name='action'><input name='sesskey' value='a&amp;b'>
          <input name='disabled' value='x' disabled><input type='checkbox' name='c' checked>
          <input type='checkbox' name='unchecked'><input type='submit' name='cancel' value='Cancel'>
          <textarea name='onlinetext_editor[text]'>A &amp; B\n&lt;x&gt;</textarea>
          <select name='format'><option value='0'>One</option><option selected value='1'>Two</option></select>
          <button name='submitbutton' value='Save'>Save</button></FORM>"""
        with patch('engine.parse', side_effect=AssertionError('must not build a DOM')):
            form = engine.find_form_fast(text, 'savesubmission')
        self.assertEqual(engine.form_data(form), {'action': 'savesubmission', 'sesskey': 'a&b', 'c': 'on',
            'onlinetext_editor[text]': 'A & B\n<x>', 'format': '1', 'submitbutton': 'Save'})

    def test_precision_settings_restore_on_error(self):
        before = gc.isenabled()
        with self.assertRaises(RuntimeError):
            with PrecisionWindow():
                self.assertFalse(gc.isenabled())
                raise RuntimeError()
        self.assertEqual(gc.isenabled(), before)

    def test_aim_does_not_follow_wall_clock_jumps(self):
        aim = AimClock(time.time() + 5)
        with patch('runtime.time.time', return_value=0):
            self.assertAlmostEqual(aim.relative(), -5, delta=0.1)


class LocalTransportTests(unittest.TestCase):
    def test_real_prepared_requests_save_then_submit_to_local_fixture(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                data = parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
                received.append(data)
                function = data['wsfunction'][0]
                result = {'lastattempt': {'submission': {'status': 'submitted', 'timemodified': 42}}} if function == 'mod_assign_get_submission_status' else []
                payload = json.dumps(result).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch('engine.BASE', f'http://127.0.0.1:{server.server_port}'):
                b = engine.Hebat('fixture', 'fixture')
                try:
                    b.token, b.sesskey, b.user_id = 'fixture-token', 'fixture-session', 1
                    b.ws_functions = {'mod_assign_get_submission_status'}
                    r = engine.Runner(b, job(), Mock())
                    r.go = time.time() - 0.2
                    r.ws_info = {'id': 7, 'drafts': True, 'rules': {'timelimit': 0}}
                    r.before_status = {'state': 'new'}
                    r.prepare_requests(123)
                    r.warm = Mock()
                    r.wait_for_form(True, 123, True)
                    self.assertEqual(r.final_state(b), 'submitted')
                    self.assertEqual([d['wsfunction'][0] for d in received],
                                     ['mod_assign_save_submission', 'mod_assign_submit_for_grading', 'mod_assign_get_submission_status'])
                    self.assertEqual(received[0]['plugindata[files_filemanager]'], ['123'])
                    self.assertEqual(received[0]['plugindata[onlinetext_editor][text]'], ['<p>My own assignment</p>'])
                    self.assertEqual(received[1]['acceptsubmissionstatement'], ['1'])
                    self.assertEqual(b.s.cookies.get_dict(), {})
                finally:
                    b.s.close()
                    b.ws_s.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
