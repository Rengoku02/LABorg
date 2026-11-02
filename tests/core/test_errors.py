from apps.core.errors import init_error_tracking, scrub_event


def test_events_leave_without_tokens_searches_or_people():
    event = {
        "request": {
            "url": "https://labstock.test/s?q=secret+project",
            "query_string": "q=secret+project",
            "cookies": {"sessionid": "abc"},
            "data": {"remarks": "private"},
            "headers": {"Cookie": "x", "Access-Token": "tok", "User-Agent": "Firefox"},
        },
        "user": {"email": "priya@lab.test"},
        "message": "failed on /a/SIGNEDTOKEN/cancel/ and /claims/t/OTHER/ with Access-Token: qtok",
        "breadcrumbs": {"values": [{"message": "GET /s?q=my+search 200"}]},
    }
    scrubbed = scrub_event(event)
    request = scrubbed["request"]
    assert request["url"] == "https://labstock.test/s"
    assert request["query_string"] == "" and "cookies" not in request and "data" not in request
    assert request["headers"] == {"User-Agent": "Firefox"}
    assert "user" not in scrubbed
    text = str(scrubbed)
    for secret in ("SIGNEDTOKEN", "OTHER", "qtok", "my+search", "secret", "priya"):
        assert secret not in text, secret


def test_no_dsn_no_tracking():
    assert init_error_tracking("") is False
