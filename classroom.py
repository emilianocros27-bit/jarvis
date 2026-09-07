"""Read-only Google Classroom access for Jarvis.

Never requests write scopes and never calls any mutating Classroom
endpoint (no turning in, editing, or grading coursework — ever).
"""
import datetime

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from google_auth import get_credentials

# submission states that mean "not turned in yet"
PENDING_STATES = {"NEW", "CREATED", "RECLAIMED_BY_STUDENT"}


class ClassroomError(Exception):
    pass


def _format_due(course_work):
    due_date = course_work.get("dueDate")
    if not due_date:
        return None, "sin fecha límite", None
    due_time = course_work.get("dueTime") or {}
    dt = datetime.date(due_date["year"], due_date["month"], due_date["day"])
    label = dt.strftime("%d/%m/%Y")
    if due_time:
        label += f" {due_time.get('hours', 0):02d}:{due_time.get('minutes', 0):02d}"
    sort_key = (dt.isoformat(), due_time.get("hours", 0), due_time.get("minutes", 0))
    due_dt = datetime.datetime(
        dt.year, dt.month, dt.day,
        due_time.get("hours", 23), due_time.get("minutes", 59),
    )
    return sort_key, label, due_dt.isoformat()


def get_pending_assignments():
    """Returns a list of {course_name, assignments: [{title, due_label}]}
    for each active course that has at least one pending assignment.
    Read-only: only .list()/.get() calls against the Classroom API."""
    try:
        service = build("classroom", "v1", credentials=get_credentials())
    except Exception as e:
        raise ClassroomError(str(e))

    try:
        courses = (
            service.courses()
            .list(courseStates=["ACTIVE"], studentId="me")
            .execute()
            .get("courses", [])
        )

        results = []
        for course in courses:
            course_id = course["id"]

            coursework_by_id = {}
            page_token = None
            while True:
                resp = (
                    service.courses()
                    .courseWork()
                    .list(courseId=course_id, pageToken=page_token)
                    .execute()
                )
                for cw in resp.get("courseWork", []):
                    coursework_by_id[cw["id"]] = cw
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
            if not coursework_by_id:
                continue

            submissions = []
            page_token = None
            while True:
                resp = (
                    service.courses()
                    .courseWork()
                    .studentSubmissions()
                    .list(
                        courseId=course_id,
                        courseWorkId="-",
                        userId="me",
                        pageToken=page_token,
                    )
                    .execute()
                )
                submissions.extend(resp.get("studentSubmissions", []))
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

            pending = []
            for sub in submissions:
                if sub.get("assignedGrade") is not None:
                    continue
                if sub.get("state") not in PENDING_STATES:
                    continue
                cw = coursework_by_id.get(sub.get("courseWorkId"))
                if not cw:
                    continue
                sort_key, due_label, due_iso = _format_due(cw)
                pending.append({
                    "title": cw.get("title", "(sin título)"),
                    "due_label": due_label,
                    "due_iso": due_iso,
                    "sort_key": sort_key,
                })

            if pending:
                pending.sort(key=lambda a: (a["sort_key"] is None, a["sort_key"]))
                for a in pending:
                    del a["sort_key"]
                results.append({
                    "course_name": course.get("name", "(curso sin nombre)"),
                    "assignments": pending,
                })

        return results
    except HttpError as e:
        raise ClassroomError(str(e))
