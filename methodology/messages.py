def split_plain(text, limit=3900):
    """Plain text avoids untrusted HTML; count UTF-16 units for Telegram compatibility."""
    while text:
        units = 0; cut = 0
        for char in text:
            size = 2 if ord(char) > 0xffff else 1
            if units + size > limit:
                break
            units += size; cut += 1
        if cut < len(text):
            boundary = text.rfind('\n', 0, cut)
            if boundary > 0:
                cut = boundary + 1
        yield text[:cut]
        text = text[cut:]
