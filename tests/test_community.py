"""Leaderboard and profile rules (section 09)."""

from __future__ import annotations


def _earn(relay, publisher, worker, out_tokens):
    t = relay.create(publisher, budget_rp=10).json()["task_id"]
    dev_id, dev = relay.pair(worker)
    relay.claim(worker, t, dev_id)
    run = relay.next(dev)
    relay.start(dev, run)
    relay.finish(dev, run, usage={"status": "known", "input_tokens": 0, "output_tokens": out_tokens})


def test_leaderboard_and_showcase(relay, clock):
    workers = [relay.login(f"w{i}") for i in range(4)]
    for i, w in enumerate(workers):
        clock.advance(1)
        # Fresh publisher each time: a publisher in debt cannot publish again.
        _earn(relay, relay.login(f"pub{i}"), w, out_tokens=1000 * (4 - i))  # w0 earns most (capped at 10 RP)

    relay.c.put("/v2/me/profile", headers=workers[0],
                json={"display_name": "Ada", "bio": "hi", "work_title": "Proj", "work_url": "https://example.org", "public": True})
    relay.c.put("/v2/me/profile", headers=workers[3],
                json={"display_name": "Dee", "work_url": "https://d.example", "public": True})
    board = relay.c.get("/v2/community").json()["leaderboard"]
    assert [e["earned_milli"] for e in board] == [10000, 9000, 6000, 3000]
    assert board[0]["name"] == "Ada" and board[0]["showcase"]["work_url"] == "https://example.org"
    assert board[1]["name"].startswith("匿名成员") and "showcase" not in board[1]
    assert board[3]["name"] == "Dee" and "showcase" not in board[3]  # rank 4 never shows work

    # Turning the profile off removes bio and link immediately.
    relay.c.put("/v2/me/profile", headers=workers[0], json={"display_name": "Ada", "public": False})
    board = relay.c.get("/v2/community").json()["leaderboard"]
    assert board[0]["name"].startswith("匿名成员") and "showcase" not in board[0]


def test_profile_validation(relay):
    u = relay.login("ada")
    assert relay.c.put("/v2/me/profile", headers=u, json={"work_url": "http://x.org"}).status_code == 422
    assert relay.c.put("/v2/me/profile", headers=u, json={"work_url": "javascript:alert(1)"}).status_code == 422
    assert relay.c.put("/v2/me/profile", headers=u, json={"bio": "x" * 181}).status_code == 422
    assert relay.c.put("/v2/me/profile", headers=u, json={"public": True}).status_code == 422
    ok = relay.c.put("/v2/me/profile", headers=u, json={"display_name": "Ada", "bio": "中" * 180, "public": True})
    assert ok.status_code == 200 and ok.json()["version"] == 1
