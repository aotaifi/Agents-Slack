import pytest
from test_api import auth, service, setup_thread  # noqa: F401


def post(client, tid, text, **headers):
    r = client.post(f"/v1/threads/{tid}/messages", json={"text": text}, headers=headers)
    assert r.status_code == 201
    return r.json()


def search(client, pid, q, **params):
    return client.get(f"/v1/projects/{pid}/search", params={"q": q, **params})


def texts(response):
    return [i["message"]["text"] for i in response.json()["items"]]


def test_finds_by_word_and_shape_matches_thread_messages(service):  # noqa: F811
    client, *_ = service
    pid, tid = setup_thread(client)
    post(client, tid, "The gradient flow converges slowly")
    post(client, tid, "Unrelated note")
    r = search(client, pid, "GRADIENT")
    assert r.status_code == 200
    body = r.json()
    assert texts(r) == ["The gradient flow converges slowly"]
    thread = client.get(f"/v1/threads/{tid}").json()
    item = body["items"][0]
    assert item["thread"] == {
        "id": tid,
        "title": thread["title"],
        "channel_id": thread["channel_id"],
    }
    assert "gradient flow" in item["snippet"]
    listed = client.get(f"/v1/threads/{tid}/messages").json()["items"]
    assert item["message"] == listed[0]
    assert body["next_before"] is None and body["cursor"] == listed[-1]["sequence"]


def test_multiple_words_must_all_match(service):  # noqa: F811
    client, *_ = service
    pid, tid = setup_thread(client)
    post(client, tid, "alpha beta gamma")
    post(client, tid, "alpha only")
    assert texts(search(client, pid, "alpha beta")) == ["alpha beta gamma"]
    assert texts(search(client, pid, "beta   alpha")) == ["alpha beta gamma"]
    assert texts(search(client, pid, "alpha delta")) == []


def test_newest_first_and_paging(service):  # noqa: F811
    client, *_ = service
    pid, tid = setup_thread(client)
    for n in range(5):
        post(client, tid, f"needle {n}")
        post(client, tid, f"haystack {n}")
    first = search(client, pid, "needle", limit=2).json()
    assert [i["message"]["text"] for i in first["items"]] == ["needle 4", "needle 3"]
    assert first["next_before"] == first["items"][-1]["message"]["sequence"]
    second = search(client, pid, "needle", limit=2, before=first["next_before"]).json()
    assert [i["message"]["text"] for i in second["items"]] == ["needle 2", "needle 1"]
    last = search(client, pid, "needle", limit=2, before=second["next_before"]).json()
    assert [i["message"]["text"] for i in last["items"]] == ["needle 0"]
    assert last["next_before"] is None
    exact = search(client, pid, "needle", limit=5).json()
    assert len(exact["items"]) == 5 and exact["next_before"] is None


def test_validation(service):  # noqa: F811
    client, *_ = service
    pid, _ = setup_thread(client)
    assert search(client, pid, "   ").status_code == 422
    assert search(client, pid, "").status_code == 422
    assert search(client, pid, "x" * 201).status_code == 422
    assert search(client, pid, "x" * 200).status_code == 200
    assert client.get(f"/v1/projects/{pid}/search").status_code == 422
    assert search(client, pid, "x", limit=0).status_code == 422
    assert search(client, pid, "x", limit=51).status_code == 422
    assert search(client, pid, "x", before=0).status_code == 422
    assert search(client, pid, "x", limit=50, before=1).status_code == 200


def test_non_member_scoped_connection_and_other_projects(service):  # noqa: F811
    client, *_ = service
    pid, tid = setup_thread(client)
    post(client, tid, "secret finding")
    stranger = client.post("/v1/actors", json={"name": "Stranger", "kind": "human"}).json()
    assert search(client, pid, "secret").status_code == 200
    r = client.get(
        f"/v1/projects/{pid}/search", params={"q": "secret"}, headers=auth(stranger["token"])
    )
    assert r.status_code == 404
    # The stranger's own project is searchable but never shows the other project's messages.
    mine = client.post("/v1/projects", json={"name": "Mine"}, headers=auth(stranger["token"]))
    other = mine.json()["id"]
    channel = client.post(
        f"/v1/projects/{other}/channels", json={"name": "Chat"}, headers=auth(stranger["token"])
    ).json()
    thread = client.post(
        f"/v1/channels/{channel['id']}/threads",
        json={"title": "T"},
        headers=auth(stranger["token"]),
    ).json()
    post(client, thread["id"], "secret of my own", **auth(stranger["token"]))
    r = client.get(
        f"/v1/projects/{other}/search", params={"q": "secret"}, headers=auth(stranger["token"])
    )
    assert texts(r) == ["secret of my own"]
    assert texts(search(client, pid, "secret")) == ["secret finding"]

    agent = client.post("/v1/actors", json={"name": "Agent", "kind": "agent"}).json()
    for project in (pid, other):
        client.post(f"/v1/projects/{project}/members", json={"actor_id": agent["actor"]["id"]})
    connection = client.post(
        "/v1/agent-connections",
        json={"actor_id": agent["actor"]["id"], "project_id": pid, "label": "Scoped"},
    )
    assert connection.status_code == 201, connection.text
    token = connection.json()["token"]
    scoped = lambda project: client.get(  # noqa: E731
        f"/v1/projects/{project}/search", params={"q": "secret"}, headers=auth(token)
    )
    assert scoped(pid).status_code == 200
    assert scoped(other).status_code == 404


def test_messages_after_snapshot_are_not_returned(service):  # noqa: F811
    client, app, *_ = service
    pid, tid = setup_thread(client)
    post(client, tid, "visible marker")
    late = post(client, tid, "future marker")
    from agent_commons.models import Project

    with app.state.session_factory() as db:
        db.get(Project, pid).cursor = late["sequence"] - 1
        db.commit()
    assert texts(search(client, pid, "marker")) == ["visible marker"]
    assert search(client, pid, "marker").json()["cursor"] == late["sequence"] - 1


def test_wildcards_are_literal(service):  # noqa: F811
    client, *_ = service
    pid, tid = setup_thread(client)
    post(client, tid, "100% sure")
    post(client, tid, "1000 sure")
    post(client, tid, "a_b match")
    post(client, tid, "aXb match")
    assert texts(search(client, pid, "100%")) == ["100% sure"]
    assert texts(search(client, pid, "a_b")) == ["a_b match"]
    for wildcard in ("%", "_", "%%", "a%b", "_ _"):
        found = texts(search(client, pid, wildcard))
        assert set(found) <= {"100% sure", "a_b match"}, (wildcard, found)


def test_unicode_german_text(service):  # noqa: F811
    client, *_ = service
    pid, tid = setup_thread(client)
    post(client, tid, "Der Übergang zwischen den Phasen ist stetig")
    post(client, tid, "Straße und Größe")
    assert texts(search(client, pid, "Übergang")) == ["Der Übergang zwischen den Phasen ist stetig"]
    assert texts(search(client, pid, "übergang")) == ["Der Übergang zwischen den Phasen ist stetig"]
    assert texts(search(client, pid, "größe")) == ["Straße und Größe"]


def test_snippet_is_plain_text_around_match(service):  # noqa: F811
    client, *_ = service
    pid, tid = setup_thread(client)
    body = "<b>" + "lorem ipsum " * 60 + "needle " + "dolor sit " * 60
    post(client, tid, body)
    snippet = search(client, pid, "needle").json()["items"][0]["snippet"]
    assert "needle" in snippet and snippet.startswith("…") and snippet.endswith("…")
    assert len(snippet) <= 205
    short = post(client, tid, "tiny needle")
    assert short["text"] == "tiny needle"
    assert search(client, pid, "tiny").json()["items"][0]["snippet"] == "tiny needle"


@pytest.mark.parametrize("q", ["needle", "NEEDLE"])
def test_text_is_stored_unchanged(service, q):  # noqa: F811
    client, *_ = service
    pid, tid = setup_thread(client)
    raw = "```py\nprint('needle')\n``` and $x^2$ <script>"
    post(client, tid, raw)
    assert texts(search(client, pid, q)) == [raw]
