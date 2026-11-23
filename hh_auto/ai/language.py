"""Grammatical agreement in candidate-authored answers, independent of experience facts."""

from __future__ import annotations

import re


_FORMS = {
    "работал": "работала", "использовал": "использовала", "применял": "применяла",
    "внедрял": "внедряла", "внедрил": "внедрила", "настраивал": "настраивала", "настроил": "настроила",
    "разрабатывал": "разрабатывала", "разработал": "разработала", "реализовал": "реализовала",
    "реализовывал": "реализовывала", "создавал": "создавала", "создал": "создала",
    "занимался": "занималась", "участвовал": "участвовала", "отвечал": "отвечала",
    "писал": "писала", "делал": "делала", "строил": "строила", "собирал": "собирала",
    "запускал": "запускала", "тестировал": "тестировала", "изучал": "изучала",
    "проходил": "проходила", "обучался": "обучалась", "разбирался": "разбиралась",
    "получил": "получила", "получал": "получала", "окончил": "окончила", "закончил": "закончила",
    "согласился": "согласилась", "готов": "готова", "рад": "рада", "знаком": "знакома",
    "заинтересован": "заинтересована", "уверен": "уверена", "согласен": "согласна",
    "благодарен": "благодарна", "ответственен": "ответственна", "был": "была", "вёл": "вела",
}
_LOOKUP = {}
_PATTERNS = []
for _male, _female in _FORMS.items():
    _common = 0
    while _common < min(len(_male), len(_female)) and _male[_common] == _female[_common]:
        _common += 1
    _pair = (_male, _female)
    for _form in (_male, _female):
        _LOOKUP[_form] = _pair
        _PATTERNS.append(re.escape(_form))
    _ending = _female[_common:]
    _dual = re.escape(_male) + r"\(\s*-?" + re.escape(_ending) + r"\s*\)"
    _PATTERNS.extend((_dual, re.escape(_male + "/" + _female), re.escape(_male) + r"/\s*" + re.escape(_ending)))
_WORD = re.compile(r"(?<!\w)(?:" + "|".join(sorted(_PATTERNS, key=len, reverse=True)) + r")(?!\w)", re.I)
_QUOTED = re.compile(r'```[\s\S]*?```|«[^»]*»|“[^”]*”|"(?:\\.|[^"\\])*"')
_SUBJECT = re.compile(r"\b(?:команда|компания|система|платформа|организация|модель|интеграция|вакансия|"
                      r"библиотека|программа|часть|работа|функция|задача|коллега|рекрутер|сервис|продукт|"
                      r"компонент|пользователь|она|он|они)\b", re.I)
_POST_SUBJECT = re.compile(r"^\s+(?:(?:[а-яё]+(?:ая|яя|ые|ие|ое|ее|ый|ой|ий)|"
                           r"готов[аоы]?|согласн[аоы]?|знаком[аоы]?)\s+){0,2}" + _SUBJECT.pattern, re.I)
_ADVERBS = {"я", "лично", "ранее", "раньше", "также", "потом", "затем", "часто", "обычно", "уже", "успешно",
            "самостоятельно", "не", "напрямую", "сразу", "да", "нет", "и", "а", "буду", "бы", "очень", "полностью"}


def candidate_gender(cfg) -> str:
    return "female" if getattr(cfg, "candidate_gender", "male") == "female" else "male"


def candidate_gender_rule(cfg) -> str:
    if candidate_gender(cfg) == "female":
        return ("Кандидат — женщина. Пиши от её лица только в женском роде: работала, использовала, готова, знакома. "
                "Не используй двойные формы работал(а)/готов(а). Это правило относится только к речи кандидата: "
                "сохраняй грамматику других действующих лиц и цитат.")
    return ("Кандидат — мужчина. Пиши от его лица только в мужском роде: работал, использовал, готов, знаком. "
            "Не используй двойные формы работал(а)/готов(а) и женский род от лица кандидата. "
            "Это правило относится только к речи кандидата: «команда работала» и «заинтересовала вакансия» "
            "сохраняют свой грамматический род; цитаты собеседника не изменяй.")


def _first_person(before: str, after: str) -> bool:
    left = re.split(r"[.!?\n;,:]|\s+(?:но|зато)\s+", before)[-1].strip()
    if _SUBJECT.search(left):
        return False
    # In "мне предложила компания" the candidate is an object, not the actor.
    if re.search(r"^\s*(?:мне|меня|нас|нам)\b", left, re.I):
        return False
    if _POST_SUBJECT.search(after):
        return False
    if re.search(r"\bя\b", left, re.I):
        return True
    if not left or re.match(r"^(?:в|на|с|со|после|до|при)\s+", left, re.I):
        return True
    earlier = _WORD.search(left)
    if earlier and _first_person(left[:earlier.start()], left[earlier.end():]):
        return True
    words = re.findall(r"[а-яё]+", left.casefold())
    return bool(words) and all(word in _ADVERBS or word in _LOOKUP for word in words)


def normalize_candidate_text(text: str, cfg) -> str:
    """Adjust only familiar first-person predicates; quoted and third-person text stays intact."""
    target = 1 if candidate_gender(cfg) == "female" else 0
    # Mask quoted contents in place so an outer subject such as "Команда «Имя»"
    # remains visible to the following predicate while quoted words stay untouched.
    masked = list(text)
    for quoted in _QUOTED.finditer(text):
        masked[quoted.start():quoted.end()] = ["\n" if char == "\n" else " " for char in quoted.group(0)]
    context = "".join(masked)
    result, position = [], 0
    for match in _WORD.finditer(context):
        raw = text[match.start():match.end()]
        value = raw
        before, after = context[:match.start()], context[match.end():]
        base = re.split(r"[(/]", raw.casefold(), 1)[0]
        passive = (base in {"был", "была"} and re.match(r"\s+\w+(?:ан|ен|ён|ат|ят)(?:а|о|ы)?\b", after, re.I)
                   and not re.search(r"\bя\b", re.split(r"[.!?\n;,:]", before)[-1], re.I))
        if _first_person(before, after) and not passive and base in _LOOKUP:
            value = _LOOKUP[base][target]
            value = value.upper() if raw.isupper() else value.capitalize() if raw[0].isupper() else value
        result.extend((text[position:match.start()], value))
        position = match.end()
    result.append(text[position:])
    return "".join(result)
