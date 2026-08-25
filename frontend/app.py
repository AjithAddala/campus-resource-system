"""Streamlit frontend for the Campus Resource Allocation System.

Talks to the FastAPI backend over HTTP only. It holds no database
connection and no business logic: every rule this system enforces —
capacity, quota, exactly-once, schedule conflicts — is decided inside a
transaction on the server, and this page's job is to send the request and
render the answer.

That includes the errors. The API returns a machine-readable envelope,

    {"detail": {"code": "QUOTA_EXCEEDED", "message": "..."}}

and the code is the contract while the message is prose. Both are shown:
the code is what distinguishes CAPACITY_EXHAUSTED from QUOTA_EXCEEDED,
which are both 409 and have opposite remedies.
"""
from __future__ import annotations

import datetime as dt
import os
import uuid
from typing import Any

import requests
import streamlit as st

API_BASE = os.getenv("API_BASE_URL", "http://localhost:8000/api/v1")
TIMEOUT = 30

DAY_CODES = "MTWRFSU"
DAY_NAMES = {
    "M": "Mon", "T": "Tue", "W": "Wed", "R": "Thu",
    "F": "Fri", "S": "Sat", "U": "Sun",
}

st.set_page_config(page_title="Campus Resources", page_icon="🎓", layout="wide")


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


class ApiError(Exception):
    """A non-2xx response, with the coded envelope unpacked if present."""

    def __init__(self, status: int, code: str | None, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"{status} {code or ''} {message}".strip())


def _unpack(response: requests.Response) -> ApiError:
    """Turn an error response into an ApiError.

    Three shapes arrive here and all three are handled, because the
    difference is invisible until one of them shows up as a raw dict in
    the UI:

        {"detail": {"code": ..., "message": ...}}   coded failures
        {"detail": "Forbidden"}                     401 / 403, uncoded
        {"detail": [{...}, ...]}                    422 from Pydantic
    """
    try:
        detail = response.json().get("detail")
    except ValueError:
        return ApiError(response.status_code, None, response.text[:400])

    if isinstance(detail, dict):
        return ApiError(
            response.status_code, detail.get("code"), detail.get("message", "")
        )
    if isinstance(detail, list):
        parts = []
        for item in detail:
            loc = ".".join(str(x) for x in item.get("loc", [])[1:])
            parts.append(f"{loc}: {item.get('msg', '')}".strip(": "))
        return ApiError(response.status_code, "VALIDATION_ERROR", "; ".join(parts))
    return ApiError(response.status_code, None, str(detail))


def api(
    method: str,
    path: str,
    *,
    json: Any = None,
    data: Any = None,
    params: dict | None = None,
    headers: dict | None = None,
    auth: bool = True,
) -> Any:
    """One request. Returns the decoded body, or raises ApiError."""
    hdrs = dict(headers or {})
    if auth and st.session_state.get("token"):
        hdrs["Authorization"] = f"Bearer {st.session_state['token']}"

    try:
        response = requests.request(
            method,
            f"{API_BASE}{path}",
            json=json,
            data=data,
            params=params,
            headers=hdrs,
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise ApiError(0, "UNREACHABLE", f"Could not reach the API at {API_BASE} — {exc}")

    if response.status_code >= 400:
        raise _unpack(response)
    return response.json() if response.content else None


def call(method: str, path: str, **kwargs) -> Any:
    """`api`, but returning the error instead of raising it.

    Streamlit reruns the whole script on every interaction, so an
    exception escaping a handler blanks the page. Callers use this and
    render the failure in place.
    """
    try:
        return api(method, path, **kwargs), None
    except ApiError as exc:
        return None, exc


def show_error(exc: ApiError) -> None:
    if exc.code:
        st.error(f"**{exc.code}** ({exc.status})\n\n{exc.message}")
    else:
        st.error(f"**{exc.status}** — {exc.message}")


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def init_state() -> None:
    st.session_state.setdefault("token", None)
    st.session_state.setdefault("user", None)
    # No GET /me/reservations exists, so a reservation id is only ever
    # seen once: in the 201 that created it. Remembered here so the
    # cancel controls have something to offer. See the note in the UI.
    st.session_state.setdefault("my_gpu_reservations", [])
    st.session_state.setdefault("my_room_reservations", [])


def logout() -> None:
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    init_state()


def is_admin() -> bool:
    return (st.session_state.get("user") or {}).get("role") == "ADMIN"


def refresh_user() -> None:
    me, err = call("GET", "/me")
    if err is None:
        st.session_state["user"] = me


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def login_form() -> None:
    st.subheader("Sign in")
    with st.form("login"):
        email = st.text_input("Email", value="student@iitk.ac.in")
        password = st.text_input("Password", type="password", value="campus123")
        submitted = st.form_submit_button("Sign in", use_container_width=True)

    if not submitted:
        return

    # OAuth2 password flow: form-encoded, not JSON.
    token, err = call(
        "POST",
        "/auth/login",
        data={"username": email, "password": password},
        auth=False,
    )
    if err:
        show_error(err)
        return

    st.session_state["token"] = token["access_token"]
    refresh_user()
    st.rerun()


def register_form() -> None:
    st.subheader("Create an account")
    with st.form("register"):
        name = st.text_input("Name")
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        role = st.selectbox("Role", ["STUDENT", "FACULTY", "ADMIN"])
        submitted = st.form_submit_button("Register", use_container_width=True)

    if not submitted:
        return

    _, err = call(
        "POST",
        "/auth/register",
        json={"name": name, "email": email, "password": password, "role": role},
        auth=False,
    )
    if err:
        show_error(err)
    else:
        st.success("Account created. Sign in on the other tab.")


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def page_dashboard() -> None:
    user = st.session_state["user"]
    st.header(f"Hello, {user['name']}")

    left, right = st.columns([1, 2])
    with left:
        st.metric("Role", user["role"])
        st.caption(f"User id {user['id']} · {user['email']}")

    with right:
        st.subheader("Your entitlements")
        quota, err = call("GET", "/me/quota")
        if err:
            show_error(err)
            return

        rows = []
        for item in quota["quotas"]:
            if not item["configured"]:
                limit = "not configured"
            elif item["unlimited"]:
                limit = "unlimited"
            else:
                limit = str(item["limit"])
            rows.append(
                {
                    "Resource": item["resource_type"],
                    "Held": item["held"],
                    "Limit": limit,
                }
            )
        st.dataframe(rows, use_container_width=True, hide_index=True)

        st.caption(
            "Held is recomputed on the server under a lock on your own user "
            "row — it is not a stored counter, so it cannot drift."
        )


def page_gpus() -> None:
    st.header("GPU clusters")

    clusters, err = call("GET", "/gpus")
    if err:
        show_error(err)
        return

    if not clusters:
        st.info("No clusters exist yet.")
        return

    st.dataframe(
        [
            {
                "id": c["id"],
                "Name": c["name"],
                "Status": c["status"],
                "Total": c["gpu_count"],
                "Allocated": c["allocated"],
                "Free": c["gpu_count"] - c["allocated"],
            }
            for c in clusters
        ],
        use_container_width=True,
        hide_index=True,
    )

    st.divider()
    st.subheader("Reserve capacity")

    labels = {f"{c['id']} — {c['name']}": c["id"] for c in clusters}
    chosen = st.selectbox("Cluster", list(labels), key="gpu_pick")
    gpu_id = labels[chosen]

    count = st.number_input("GPU units", min_value=1, max_value=64, value=1, step=1)

    use_key = st.checkbox(
        "Send an Idempotency-Key",
        value=True,
        help=(
            "With a key, sending the same request twice books once and "
            "returns the original response. Without one, each send is a "
            "separate booking — that is the honest default, not a bug."
        ),
    )

    if "idem_key" not in st.session_state:
        st.session_state["idem_key"] = str(uuid.uuid4())

    if use_key:
        key_col, new_col = st.columns([4, 1])
        with key_col:
            st.code(st.session_state["idem_key"], language=None)
        with new_col:
            if st.button("New key", use_container_width=True):
                st.session_state["idem_key"] = str(uuid.uuid4())
                st.rerun()
        st.caption(
            "Press Reserve twice with the same key to see exactly-once: the "
            "second press returns the first reservation, not a second one."
        )

    if st.button("Reserve", type="primary"):
        headers = {"Idempotency-Key": st.session_state["idem_key"]} if use_key else {}
        reservation, err = call(
            "POST",
            f"/gpus/{gpu_id}/reservations",
            json={"gpu_count": int(count)},
            headers=headers,
        )
        if err:
            show_error(err)
        else:
            st.success(
                f"Reservation {reservation['id']} — {reservation['gpu_count']} "
                f"unit(s) on cluster {reservation['gpu_cluster_id']}"
            )
            st.json(reservation)
            known = st.session_state["my_gpu_reservations"]
            if not any(r["id"] == reservation["id"] for r in known):
                known.append(reservation)

    st.divider()
    st.subheader("Release")
    _release_gpu_controls()


def _release_gpu_controls() -> None:
    known = st.session_state["my_gpu_reservations"]
    if known:
        labels = {
            f"#{r['id']} — {r['gpu_count']} unit(s) on cluster {r['gpu_cluster_id']}": r
            for r in known
        }
        pick = st.selectbox("Reservation", list(labels), key="gpu_release_pick")
        target = labels[pick]
        res_id, cluster_id = target["id"], target["gpu_cluster_id"]
    else:
        st.caption(
            "Nothing reserved in this browser session. The API has no "
            "endpoint that lists your existing reservations, so enter the "
            "ids by hand to release one made earlier."
        )
        cluster_id = st.number_input("Cluster id", min_value=1, step=1, key="rel_cl")
        res_id = st.number_input("Reservation id", min_value=1, step=1, key="rel_rs")

    if st.button("Release"):
        released, err = call(
            "DELETE", f"/gpus/{int(cluster_id)}/reservations/{int(res_id)}"
        )
        if err:
            show_error(err)
        else:
            st.success(f"Released reservation {released['id']}.")
            st.session_state["my_gpu_reservations"] = [
                r for r in known if r["id"] != released["id"]
            ]


def page_rooms() -> None:
    st.header("Rooms")

    rooms, err = call("GET", "/rooms")
    if err:
        show_error(err)
        return

    if not rooms:
        st.info("No rooms exist yet.")
        return

    st.dataframe(
        [
            {
                "id": r["id"],
                "Name": r["name"],
                "Building": r["building"],
                "Seats": r["capacity"],
                "Status": r["status"],
            }
            for r in rooms
        ],
        use_container_width=True,
        hide_index=True,
    )

    labels = {f"{r['id']} — {r['name']}": r["id"] for r in rooms}
    chosen = st.selectbox("Room", list(labels), key="room_pick")
    room_id = labels[chosen]

    st.divider()
    st.subheader("Pick a window")

    col_a, col_b, col_c = st.columns(3)
    with col_a:
        day = st.date_input("Date", value=dt.date.today() + dt.timedelta(days=1))
    with col_b:
        start_t = st.time_input("From", value=dt.time(10, 0))
    with col_c:
        end_t = st.time_input("To", value=dt.time(12, 0))

    start = dt.datetime.combine(day, start_t, tzinfo=dt.timezone.utc)
    end = dt.datetime.combine(day, end_t, tzinfo=dt.timezone.utc)

    if end <= start:
        st.warning("The end of the window must be after its start.")
        return

    check_col, book_col = st.columns(2)

    with check_col:
        if st.button("Check availability", use_container_width=True):
            avail, err = call(
                "GET",
                f"/rooms/{room_id}/availability",
                params={"start": start.isoformat(), "end": end.isoformat()},
            )
            if err:
                show_error(err)
            elif avail["available"]:
                st.success("Free — as far as this read can tell.")
            else:
                st.warning("Already booked in that window:")
                st.dataframe(
                    [
                        {"id": c["id"], "From": c["start_time"], "To": c["end_time"]}
                        for c in avail["conflicts"]
                    ],
                    use_container_width=True,
                    hide_index=True,
                )

    with book_col:
        if st.button("Book it", type="primary", use_container_width=True):
            reservation, err = call(
                "POST",
                f"/rooms/{room_id}/reservations",
                json={"start_time": start.isoformat(), "end_time": end.isoformat()},
            )
            if err:
                show_error(err)
            else:
                st.success(f"Booked — reservation {reservation['id']}.")
                st.json(reservation)
                st.session_state["my_room_reservations"].append(reservation)

    st.caption(
        "Availability is advisory: it is read without a lock and is stale the "
        "moment it returns. The booking itself re-checks under an exclusion "
        "constraint in the database, which is what actually decides a race."
    )

    st.divider()
    st.subheader("Cancel a booking")
    known = st.session_state["my_room_reservations"]
    if known:
        labels = {f"#{r['id']} — {r['start_time']} → {r['end_time']}": r for r in known}
        pick = st.selectbox("Booking", list(labels), key="room_cancel_pick")
        target = labels[pick]
        res_id, res_room = target["id"], target["resource_id"]
    else:
        st.caption(
            "Nothing booked in this session — enter the ids by hand. "
            "Checking availability above shows the id of any conflicting "
            "booking, which is one way to find your own."
        )
        res_room = st.number_input("Room id", min_value=1, step=1, key="rc_room")
        res_id = st.number_input("Reservation id", min_value=1, step=1, key="rc_res")

    if st.button("Cancel booking"):
        cancelled, err = call(
            "DELETE", f"/rooms/{int(res_room)}/reservations/{int(res_id)}"
        )
        if err:
            show_error(err)
        else:
            st.success(f"Cancelled reservation {cancelled['id']}.")
            st.session_state["my_room_reservations"] = [
                r for r in known if r["id"] != cancelled["id"]
            ]


def _describe_days(days: str) -> str:
    return " ".join(DAY_NAMES.get(ch, ch) for ch in days)


def page_courses() -> None:
    st.header("Courses")

    courses, err = call("GET", "/courses")
    if err:
        show_error(err)
        return

    if not courses:
        st.info("No courses exist yet. An admin can create one under Admin.")
        return

    labels = {f"{c['code']} — {c['name']}": c["id"] for c in courses}
    chosen = st.selectbox("Course", list(labels), key="course_pick")
    course_id = labels[chosen]

    offerings, err = call("GET", f"/courses/{course_id}/offerings")
    if err:
        show_error(err)
        return

    if not offerings:
        st.info("This course has no offerings yet.")
        return

    st.dataframe(
        [
            {
                "id": o["id"],
                "Term": f"{o['semester']} {o['year']}",
                "Meets": f"{_describe_days(o['days'])} {o['start_time']}–{o['end_time']}",
                "Capacity": o["capacity"],
                "Enrolled": o["enrolled_count"],
                "Free": o["seats_available"],
            }
            for o in offerings
        ],
        use_container_width=True,
        hide_index=True,
    )

    offer_labels = {
        f"#{o['id']} — {o['semester']} {o['year']} ({o['seats_available']} free)": o
        for o in offerings
    }
    pick = st.selectbox("Offering", list(offer_labels), key="offer_pick")
    offering = offer_labels[pick]
    offering_id = offering["id"]

    st.divider()
    reg_col, drop_col = st.columns(2)

    with reg_col:
        if st.button("Register", type="primary", use_container_width=True):
            enrollment, err = call("POST", f"/offerings/{offering_id}/register")
            if err:
                show_error(err)
                if err.code == "OFFERING_FULL":
                    st.info("Full — you can queue for it under Waitlist.")
            else:
                st.success(f"Enrolled — enrollment {enrollment['id']}.")

    with drop_col:
        if st.button("Drop", use_container_width=True):
            dropped, err = call("DELETE", f"/offerings/{offering_id}/drop")
            if err:
                show_error(err)
            else:
                st.success(f"Dropped — enrollment {dropped['id']} is now DROPPED.")
                st.caption(
                    "If anyone was queued for this section, the seat you just "
                    "freed was handed to the next eligible student inside the "
                    "same transaction."
                )

    st.divider()
    st.subheader("Waitlist")
    _waitlist_controls(offering_id)


def _waitlist_controls(offering_id: int) -> None:
    queue, err = call("GET", f"/offerings/{offering_id}/waitlist")
    if err:
        show_error(err)
    elif not queue:
        st.caption("Nobody is queued for this offering.")
    else:
        me = st.session_state["user"]["id"]
        st.dataframe(
            [
                {
                    "Position": e["position"],
                    "Student": e["student_id"],
                    "You": "←" if e["student_id"] == me else "",
                    "Joined": e["created_at"],
                }
                for e in queue
            ],
            use_container_width=True,
            hide_index=True,
        )

    join_col, leave_col = st.columns(2)

    with join_col:
        if st.button("Join the queue", use_container_width=True):
            entry, err = call("POST", f"/offerings/{offering_id}/waitlist")
            if err:
                show_error(err)
                if err.code == "OFFERING_NOT_FULL":
                    st.info(
                        "There are still seats — register instead. A queue for "
                        "an available seat would never be promoted, because "
                        "promotion only fires when somebody drops."
                    )
            else:
                st.success(f"Queued at position {entry['position']}.")

    with leave_col:
        if st.button("Leave the queue", use_container_width=True):
            left, err = call("DELETE", f"/offerings/{offering_id}/waitlist")
            if err:
                show_error(err)
            else:
                st.success(f"Left the queue (entry {left['id']}).")

    st.caption(
        "Positions are computed when read, never stored — everyone behind a "
        "departing student moves up automatically."
    )


def page_admin() -> None:
    st.header("Admin")

    tabs = st.tabs(["Resources", "Catalogue", "Quota policy"])

    with tabs[0]:
        st.subheader("Create a GPU cluster")
        with st.form("new_cluster"):
            name = st.text_input("Cluster name")
            units = st.number_input("GPU units", min_value=1, value=8, step=1)
            if st.form_submit_button("Create cluster"):
                created, err = call(
                    "POST", "/gpus", json={"name": name, "gpu_count": int(units)}
                )
                if err:
                    show_error(err)
                else:
                    st.success(f"Created cluster {created['id']}.")

        st.subheader("Create a room")
        with st.form("new_room"):
            rname = st.text_input("Room name")
            building = st.text_input("Building")
            seats = st.number_input("Seats", min_value=1, value=30, step=1)
            if st.form_submit_button("Create room"):
                created, err = call(
                    "POST",
                    "/rooms",
                    json={
                        "name": rname,
                        "building": building,
                        "capacity": int(seats),
                    },
                )
                if err:
                    show_error(err)
                else:
                    st.success(f"Created room {created['id']}.")

        st.subheader("Take a resource out of service")
        st.caption(
            "BLOCKED stops *new* allocations and does not evict existing "
            "ones — anyone already holding the resource keeps it."
        )
        kind = st.radio("Kind", ["GPU cluster", "Room"], horizontal=True)
        target_id = st.number_input("Resource id", min_value=1, step=1, key="blk_id")
        new_status = st.selectbox("Status", ["AVAILABLE", "BLOCKED"])
        if st.button("Apply status"):
            path = "/gpus" if kind == "GPU cluster" else "/rooms"
            updated, err = call(
                "PATCH", f"{path}/{int(target_id)}", json={"status": new_status}
            )
            if err:
                show_error(err)
            else:
                st.success(f"{updated['name']} is now {updated['status']}.")

    with tabs[1]:
        st.subheader("Create a course")
        with st.form("new_course"):
            code = st.text_input("Code", placeholder="CS641")
            cname = st.text_input("Name", placeholder="Modern Cryptography")
            if st.form_submit_button("Create course"):
                created, err = call(
                    "POST", "/courses", json={"code": code, "name": cname}
                )
                if err:
                    show_error(err)
                else:
                    st.success(f"Created course {created['id']} ({created['code']}).")

        st.subheader("Create an offering")
        with st.form("new_offering"):
            course_id = st.number_input("Course id", min_value=1, step=1)
            instructor_id = st.number_input(
                "Instructor id", min_value=1, step=1,
                help="Must be a FACULTY user, or the API returns INSTRUCTOR_NOT_FACULTY.",
            )
            semester = st.selectbox("Semester", ["AUTUMN", "SPRING", "SUMMER"])
            year = st.number_input("Year", min_value=2000, max_value=2100, value=2026)
            days = st.multiselect(
                "Days", list(DAY_CODES), default=["M", "W", "F"],
                format_func=lambda d: DAY_NAMES[d],
            )
            time_a, time_b = st.columns(2)
            with time_a:
                o_start = st.time_input("Starts", value=dt.time(9, 0), key="off_start")
            with time_b:
                o_end = st.time_input("Ends", value=dt.time(10, 30), key="off_end")
            capacity = st.number_input("Capacity", min_value=1, value=50, step=1)

            if st.form_submit_button("Create offering"):
                created, err = call(
                    "POST",
                    "/offerings",
                    json={
                        "course_id": int(course_id),
                        "instructor_id": int(instructor_id),
                        "semester": semester,
                        "year": int(year),
                        # Zero-padded "HH:MM" — the server compares these
                        # lexicographically for schedule clashes.
                        "start_time": o_start.strftime("%H:%M"),
                        "end_time": o_end.strftime("%H:%M"),
                        "days": "".join(days),
                        "capacity": int(capacity),
                    },
                )
                if err:
                    show_error(err)
                else:
                    st.success(f"Created offering {created['id']}.")

    with tabs[2]:
        st.subheader("Per-role entitlements")
        st.caption(
            "Policy, not per-user state. Raising a limit takes effect on the "
            "next allocation; it never invalidates one already granted."
        )
        role = st.selectbox("Role", ["STUDENT", "FACULTY", "ADMIN"], key="q_role")
        rtype = st.selectbox("Resource", ["GPU", "ROOM", "COURSE"], key="q_type")

        current, err = call("GET", f"/admin/quotas/{role}/{rtype}")
        if err:
            if err.status == 404:
                st.info(
                    f"No policy row for ({role}, {rtype}). The server fails "
                    "closed on a missing row — allocations are refused rather "
                    "than treated as unlimited."
                )
            else:
                show_error(err)
        else:
            limit = current["max_units"]
            st.metric("Current limit", "unlimited" if limit is None else limit)

        unlimited = st.checkbox("Unlimited", key="q_unl")
        new_limit = st.number_input(
            "Max units", min_value=0, value=2, step=1, disabled=unlimited
        )
        if st.button("Save policy"):
            payload = {"max_units": None if unlimited else int(new_limit)}
            saved, err = call("PUT", f"/admin/quotas/{role}/{rtype}", json=payload)
            if err:
                show_error(err)
            else:
                shown = "unlimited" if saved["max_units"] is None else saved["max_units"]
                st.success(f"({saved['role']}, {saved['resource_type']}) → {shown}")


# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------


def main() -> None:
    init_state()

    with st.sidebar:
        st.title("🎓 Campus Resources")
        st.caption(API_BASE)

        if st.session_state["token"] is None:
            tab_in, tab_up = st.tabs(["Sign in", "Register"])
            with tab_in:
                login_form()
            with tab_up:
                register_form()
            st.stop()

        user = st.session_state["user"]
        if user is None:
            refresh_user()
            user = st.session_state["user"]
            if user is None:
                st.warning("Session expired.")
                logout()
                st.stop()

        st.success(f"{user['name']} · {user['role']}")

        pages = {
            "Dashboard": page_dashboard,
            "GPU clusters": page_gpus,
            "Rooms": page_rooms,
            "Courses": page_courses,
        }
        if is_admin():
            pages["Admin"] = page_admin

        choice = st.radio("Go to", list(pages), label_visibility="collapsed")

        st.divider()
        if st.button("Sign out", use_container_width=True):
            logout()
            st.rerun()

    pages[choice]()


if __name__ == "__main__":
    main()
