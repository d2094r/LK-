
def remove_trailing_commas(text: str) -> str:
    result = []
    in_string = False
    escaped = False

    for i, ch in enumerate(text):
        if in_string:
            result.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
                result.append(ch)
            elif ch == ",":
                j = i + 1
                while j < len(text) and text[j].isspace():
                    j += 1
                if j < len(text) and text[j] in "]}":
                    continue
                result.append(ch)
            else:
                result.append(ch)

    return "".join(result)


def read_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return dict(default)

    original = path.read_text(encoding="utf-8")

    try:
        result = json.loads(original)
    except json.JSONDecodeError:
        corrected = remove_trailing_commas(original)

        if corrected == original:
            raise

        result = json.loads(corrected)

        backup = path.with_name(path.name + ".invalid.bak")

        try:
            if not backup.exists():
                backup.write_text(original, encoding="utf-8")
            path.write_text(corrected, encoding="utf-8")
            logger.warning("JSONの余分なカンマを修正: %s", path)
        except OSError:
            logger.warning(
                "JSONは読み込めましたが修正内容を保存できません: %s",
                path
            )

    if not isinstance(result, dict):
        raise ValueError(f"{path} の形式が不正です")

    return result
