import pytest
from conftest import setup_thread

SAMPLES = {
    # rocket, ZWJ family, flag, skin-tone modifier
    "emoji": (
        "Launch \U0001f680 family \U0001f469\u200d\U0001f469\u200d\U0001f467 "
        "flag \U0001f1e9\U0001f1ea thumbs \U0001f44d\U0001f3fd"
    ),
    # Arabic, Hebrew, Arabic-Indic digits and explicit bidi embedding controls
    "right-to-left": (
        "\u0645\u0631\u062d\u0628\u0627 \u05e9\u05dc\u05d5\u05dd abc "
        "\u0661\u0662\u0663 \u202bmark\u202c"
    ),
    # base letters with combining marks, a Devanagari conjunct, stacked diacritics
    "combining": "e\u0301 a\u0308\u0323 n\u0303 \u0915\u094d\u0937\u093f z\u0335\u0321",
    "mixed": "caf\u00e9 vs cafe\u0301 \u2603 \u4e2d\u6587 tab\there\nnewline",
}


def post(client, tid, text):
    return client.post(f"/v1/threads/{tid}/messages", json={"text": text})


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_text_round_trips_exactly(service, name):
    client, *_ = service
    pid, tid = setup_thread(client)
    text = SAMPLES[name]
    created = post(client, tid, text)
    assert created.status_code == 201
    assert created.json()["text"] == text
    (listed,) = client.get(f"/v1/threads/{tid}/messages").json()["items"]
    assert listed["text"] == text  # no normalisation of combining sequences
    events = client.get(f"/v1/projects/{pid}/events").json()["items"]
    assert [e["payload"]["text"] for e in events if e["type"] == "message.created"] == [text]


def test_composed_and_decomposed_forms_stay_distinct(service):
    client, *_ = service
    _, tid = setup_thread(client)
    post(client, tid, "café")
    post(client, tid, "café")
    texts = [m["text"] for m in client.get(f"/v1/threads/{tid}/messages").json()["items"]]
    assert texts == ["café", "café"]


def test_message_size_boundary(service):
    client, *_ = service
    pid, tid = setup_thread(client)
    big = "é\U0001f680x" * 6666 + "ab"  # 20,000 characters, many of them multi-byte
    assert len(big) == 20000
    created = post(client, tid, big)
    assert created.status_code == 201
    assert created.json()["text"] == big
    (listed,) = client.get(f"/v1/threads/{tid}/messages").json()["items"]
    assert listed["text"] == big
    rejected = post(client, tid, big + "x")
    assert rejected.status_code == 422
    assert len(client.get(f"/v1/threads/{tid}/messages").json()["items"]) == 1
