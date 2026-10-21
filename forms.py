"""Extract Moodle form controls without building a full DOM."""
import html
import re
from dataclasses import dataclass

TAG_ATTRS = r'''(?:"[^"]*"|'[^']*'|[^'">])*'''
FORMS = re.compile(r'<form\b' + TAG_ATTRS + r'>(.*?)</form\s*>', re.I | re.S)
CONTROLS = re.compile(r'<(input)\b(' + TAG_ATTRS + r')>|<(textarea|select|button)\b(' + TAG_ATTRS +
                      r')>(.*?)</\3\s*>', re.I | re.S)
ATTRS = re.compile(r'''([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?''', re.S)
OPTIONS = re.compile(r'<option\b([^>]*)>(.*?)(?=</option|<option|$)', re.I | re.S)
TAG = re.compile(r'<[^>]*>')


def attributes(text):
    return {m[0].lower(): html.unescape(next((v for v in m[1:] if v), '')) for m in ATTRS.findall(text)}


@dataclass
class FastForm:
    data: dict
    controls: set


def extract_form(text, action):
    for match in FORMS.finditer(text):
        snippet = match.group(1)
        if action not in snippet:
            continue
        data, names = {}, set()
        for control in CONTROLS.finditer(snippet):
            tag = (control[1] or control[3]).lower()
            attrs = attributes(control[2] if control[1] else control[4])
            name = attrs.get('name')
            if not name:
                continue
            names.add(name)
            if 'disabled' in attrs:
                continue
            if tag == 'textarea':
                data[name] = html.unescape(control[5])
            elif tag == 'select':
                options = [(attributes(a), html.unescape(TAG.sub('', t))) for a, t in OPTIONS.findall(control[5])]
                options = [(a, t) for a, t in options if 'disabled' not in a]
                if options:
                    a, t = next(((a, t) for a, t in options if 'selected' in a), options[0])
                    data[name] = a.get('value', t)
            else:
                kind = attrs.get('type', 'submit' if tag == 'button' else 'text').lower()
                if kind in ('button', 'image', 'reset', 'file') or (kind == 'submit' and name != 'submitbutton'):
                    continue
                if kind in ('checkbox', 'radio') and 'checked' not in attrs:
                    continue
                data[name] = attrs.get('value', 'on' if kind in ('checkbox', 'radio') else '')
        if data.get('action') == action:
            return FastForm(data, names)
    return None
