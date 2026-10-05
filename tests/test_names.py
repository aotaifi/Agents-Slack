from concurrent.futures import ThreadPoolExecutor

from conftest import auth, setup_thread


def test_rename_preserves_research_and_emits_only_changes(service):
    client, _, _, _ = service
    p = client.post("/v1/projects", json={"name": "Original", "description": "Keep project"}).json()
    pid = p["id"]
    c = client.post(
        f"/v1/projects/{pid}/channels",
        json={"name": "Original channel", "description": "Keep channel"},
    ).json()
    tid = client.post(f"/v1/channels/{c['id']}/threads", json={"title": "Research"}).json()["id"]
    message = client.post(f"/v1/threads/{tid}/messages", json={"text": "Keep findings"}).json()
    client.put(f"/v1/projects/{pid}/rules", json={"text": "Keep rules"})
    members = client.get(f"/v1/projects/{pid}/members").json()
    cursor = client.get(f"/v1/projects/{pid}/events").json()["cursor"]
    renamed = client.patch(f"/v1/projects/{pid}", json={"name": " New project "})
    assert renamed.status_code == 200 and renamed.json() == {**p, "name": "New project"}
    channel = client.patch(f"/v1/channels/{c['id']}", json={"name": "New channel"})
    assert channel.status_code == 200 and channel.json() == {**c, "name": "New channel"}
    events = client.get(f"/v1/projects/{pid}/events", params={"after": cursor}).json()
    assert [e["type"] for e in events["items"]] == ["project.updated", "channel.updated"]
    assert [e["id"] for e in events["items"]] == [cursor + 1, cursor + 2]
    assert events["items"][0]["payload"] == renamed.json()
    assert events["items"][1]["payload"] == channel.json()
    for path, name in (
        (f"/v1/projects/{pid}", "New project"),
        (f"/v1/channels/{c['id']}", "New channel"),
    ):
        assert client.patch(path, json={"name": name}).status_code == 200
    assert client.get(f"/v1/projects/{pid}/events").json()["cursor"] == cursor + 2
    assert client.get(f"/v1/projects/{pid}/members").json() == members
    assert client.get(f"/v1/projects/{pid}/rules").json() == {"text": "Keep rules", "version": 1}
    assert client.get(f"/v1/threads/{tid}/messages").json()["items"] == [message]


def test_rename_requires_project_owner_and_validates_names(service):
    client, _, _, _ = service
    pid, tid = setup_thread(client)
    cid = client.get(f"/v1/threads/{tid}").json()["channel_id"]
    guest = client.post("/v1/actors", json={"name": "Guest", "kind": "human"}).json()
    agent = client.post("/v1/actors", json={"name": "Agent", "kind": "agent"}).json()
    outsider = client.post("/v1/actors", json={"name": "Outsider", "kind": "human"}).json()
    for actor in (guest, agent):
        client.post(f"/v1/projects/{pid}/members", json={"actor_id": actor["actor"]["id"]})
    for path in (f"/v1/projects/{pid}", f"/v1/channels/{cid}"):
        for actor in (guest, agent):
            assert (
                client.patch(
                    path, json={"name": "Forbidden"}, headers=auth(actor["token"])
                ).status_code
                == 403
            )
        assert (
            client.patch(path, json={"name": "Hidden"}, headers=auth(outsider["token"])).status_code
            == 404
        )
        for body in ({"name": " "}, {"name": "x" * 201}, {"name": "New", "description": ""}):
            assert client.patch(path, json=body).status_code == 422
    assert client.get(f"/v1/projects/{pid}").json()["name"] == "Physics"
    assert client.get(f"/v1/projects/{pid}/channels").json()["items"][0]["name"] == "General"


def test_concurrent_channel_renames_keep_ordered_project_events(service):
    client, _, _, _ = service
    pid, tid = setup_thread(client)
    cid = client.get(f"/v1/threads/{tid}").json()["channel_id"]
    cursor = client.get(f"/v1/projects/{pid}/events").json()["cursor"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(
                lambda i: client.patch(f"/v1/channels/{cid}", json={"name": f"Topic {i}"}), range(4)
            )
        )
    assert all(r.status_code == 200 for r in responses)
    events = client.get(f"/v1/projects/{pid}/events", params={"after": cursor}).json()["items"]
    assert len(events) == 4
    assert [e["id"] for e in events] == list(range(cursor + 1, cursor + 5))
    assert {e["payload"]["name"] for e in events} == {f"Topic {i}" for i in range(4)}
    assert (
        client.get(f"/v1/projects/{pid}/channels").json()["items"][0]["name"]
        == events[-1]["payload"]["name"]
    )
