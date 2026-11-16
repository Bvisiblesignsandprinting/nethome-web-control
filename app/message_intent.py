"""Resolve Smart Room power intent before generic AC or AI interpretation."""
import re

SMART = r"(?:the\s+)?smart(?:\s+(?:room|control|auto|system|settings))*"
FILLER = r"(?:(?:only|just)\s+)?"
POWER = [
    re.compile(r"\b(?:turn|switch|shut)\s+(on|off)\s+" + FILLER + SMART + r"\b"),
    re.compile(r"\b(?:turn|switch|shut)\s+" + FILLER + SMART + r"\s+(on|off)\b"),
    re.compile(r"\b(enable|disable|activate|deactivate|start|stop|pause|resume)\s+" + FILLER + SMART + r"\b"),
]
ON = {'on', 'enable', 'activate', 'start', 'resume'}


def _negated(text, start):
    # Scope negation to its own command, not a separate later instruction.
    return bool(re.search(r"\b(?:do not|don't|dont|never|not)(?:\s+(?:want|you|to|please|ever|need|should|we|i|would|like|anyone|really|think))*\s+$", text[:start]))


def smart_power_intent(raw):
    """Return a canonical Smart command, a safe refusal, or None.

    A recognized Smart request never falls through to appliance power parsing.
    Ambiguous/negated compound instructions never actuate either system.
    """
    text = ' '.join(str(raw).lower().replace('’', "'").split())
    text = re.sub(r'\bsmart(room|control|auto)\b', r'smart \1', text)
    has_smart = bool(re.search(r'\bsmart\b', text))
    if not has_smart:
        if re.search(r"\b(?:do not|don't|dont|never)\b.*\b(?:turn|switch|shut|power|stop)\b", text):
            return 'NO CHANGE'
        return None
    if re.match(r'^(?:why|what happens|how|when|should|would|can i|could i|do i|does|will)\b', text) and re.search(r'\b(?:turn|switch|disable|enable|stop|start)\b', text):
        return 'CLARIFY'
    requests = []
    for pattern in POWER:
        requests.extend(m.group(1) in ON for m in pattern.finditer(text) if not _negated(text, m.start()))
    short = re.fullmatch(r"(?:please\s+)?" + SMART + r"\s+(on|off|enabled|disabled)(?:\s+please)?[.!]?", text)
    if short:
        requests.append(short.group(1) in {'on', 'enabled'})
    if requests:
        # Smart scheduling is not implemented. Do not mistake a future request
        # for an immediate power command on the AC.
        if re.search(r'\b(?:at|after|until|later|tomorrow|tonight|schedule|scheduled)\b|\bin\s+[\w -]+\s+(?:minutes?|hours?|days?)\b', text):
            return 'CLARIFY'
        ac_power = re.compile(r'\b(?:turn|switch|shut|power)\s+(?:on|off)\s+(?:the\s+)?(?:ac|machine|unit|air conditioner)\b|\b(?:turn|switch|shut|power)\s+(?:the\s+)?(?:ac|machine|unit|air conditioner)\s+(?:on|off)\b')
        if any(not _negated(text, m.start()) for m in ac_power.finditer(text)) or len(set(requests)) > 1:
            return 'CLARIFY'
        return 'SMART ON' if requests[0] else 'SMART OFF'
    if re.search(r'\b(?:on|off|enable|disable|activate|deactivate|start|stop|pause|resume|turn|switch|shut|power)\b', text):
        if re.match(r'^(?:is|are|was|what|why|how|check|show)\b', text) and not re.search(r'\b(?:turn|switch|shut)\b', text):
            return 'SMART STATUS'
        return 'NO CHANGE' if re.search(r"\b(?:do not|don't|dont|never|not)\b", text) else 'CLARIFY'
    return None
