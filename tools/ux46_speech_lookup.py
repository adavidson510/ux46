"""Look up an exact message in the same paged history used by the UI."""


class SpeechLookupError(Exception):
    def __init__(self, message, code='item_unknown'):
        super().__init__(message)
        self.code = code


def find_message(read_page, item_id):
    cursor = None
    seen = set()
    for _ in range(20):
        page = read_page(cursor)
        if page.get('unavailable') or not isinstance(page.get('items'), list):
            raise SpeechLookupError('Conversation history could not be checked. Try again.', 'history_unavailable')
        for item in page['items']:
            if str(item.get('id') or '') == item_id:
                return item
        cursor = page.get('next_cursor')
        if not cursor or cursor in seen:
            break
        seen.add(cursor)
    raise SpeechLookupError('This reply could not be found in the available history. Retry or skip it.')
