from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import auth, setup_thread

WRITERS = 4
POSTS = 20


def add_writer(client, pid, name):
    agent = client.post("/v1/actors", json={"name": name, "kind": "agent"}).json()
    added = client.post(f"/v1/projects/{pid}/members", json={"actor_id": agent["actor"]["id"]})
    assert added.status_code == 201
    return agent["token"]


def all_events(client, pid):
    events, after = [], 0
    while True:
        page = client.get(f"/v1/projects/{pid}/events", params={"after": after, "limit": 200})
        body = page.json()
        events.extend(body["items"])
        if body["next_cursor"] is None:
            return events, body["cursor"]
        after = body["next_cursor"]


@pytest.mark.parametrize("limit", [None, 15], ids=["unlimited", "rate-limited"])
def test_different_writers_posting_concurrently_keep_sequences_gap_free(service, limit):
    client, app, _, _ = service
    pid, tid = setup_thread(client)
    tokens = [add_writer(client, pid, f"Writer {n}") for n in range(WRITERS)]
    if limit:
        app.state.posting_limit = limit
    path = f"/v1/threads/{tid}/messages"
    jobs = [(w, n) for n in range(POSTS) for w in range(WRITERS)]

    def post(job):
        writer, n = job
        return client.post(
            path, json={"text": f"writer {writer} post {n}"}, headers=auth(tokens[writer])
        )

    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        results = list(pool.map(post, jobs))

    statuses = [r.status_code for r in results]
    assert set(statuses) <= {201, 429}, statuses  # no 500s, no other failures
    created = [r.json() for r in results if r.status_code == 201]
    if limit:
        assert len(created) == WRITERS * limit
        assert statuses.count(429) == WRITERS * (POSTS - limit)
        assert all(int(r.headers["Retry-After"]) > 0 for r in results if r.status_code == 429)
    else:
        assert len(created) == WRITERS * POSTS

    sequences = [m["sequence"] for m in created]
    assert len(set(sequences)) == len(sequences)  # unique per project
    assert len({m["id"] for m in created}) == len(created)
    listed = client.get(path, params={"limit": 200}).json()["items"]
    assert sorted(m["sequence"] for m in listed) == sorted(sequences)
    assert sorted(m["text"] for m in listed) == sorted(
        r.json()["text"] for r in results if r.status_code == 201
    )

    events, cursor = all_events(client, pid)
    # Every project event, including the message events, is numbered 1..cursor with no gap.
    assert [e["id"] for e in events] == list(range(1, cursor + 1))
    message_events = [e for e in events if e["type"] == "message.created"]
    assert sorted(e["id"] for e in message_events) == sorted(sequences)
    assert {e["payload"]["id"] for e in message_events} == {m["id"] for m in created}
    assert cursor == max(sequences)
