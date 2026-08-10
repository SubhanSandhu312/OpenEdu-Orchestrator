"""Deterministic dummy PIEAS data generator.

Not part of the sync pipeline itself -- this only exists to populate the
PIEAS dummy database so the four agents have something realistic to work on.
A fixed Faker seed keeps output reproducible across runs and across machines,
which the test suite relies on.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from faker import Faker

from openedu_orchestrator.models import PieasCourse, PieasFaculty, PieasStudent
from openedu_orchestrator import pieas_source as _default_source

DEPARTMENTS = [
    "Computer Science",
    "Electrical Engineering",
    "Mechanical Engineering",
    "Chemical Engineering",
    "Metallurgy & Materials Engineering",
    "Nuclear Engineering",
    "Physics",
    "Mathematics",
    "Management Sciences",
]

DEPARTMENT_CODES = {
    "Computer Science": "CS",
    "Electrical Engineering": "EE",
    "Mechanical Engineering": "ME",
    "Chemical Engineering": "CHE",
    "Metallurgy & Materials Engineering": "MME",
    "Nuclear Engineering": "NE",
    "Physics": "PHY",
    "Mathematics": "MATH",
    "Management Sciences": "MS",
}

DESIGNATIONS = [
    "Lecturer", "Assistant Professor", "Associate Professor", "Professor",
]

BATCH_YEARS = [2021, 2022, 2023, 2024, 2025]


def _random_past_timestamp(rng: random.Random, days_back: int = 400) -> datetime:
    now = datetime.now(timezone.utc)
    delta = timedelta(days=rng.randint(1, days_back), seconds=rng.randint(0, 86400))
    return now - delta


def seed_pieas(
    conn,
    num_students: int = 60,
    num_faculty: int = 18,
    num_courses: int = 14,
    seed: int = 42,
    source=_default_source,
) -> dict[str, int]:
    """`source` defaults to the SQLite-backed pieas_source module -- pass
    pieas_source_mysql to seed a real MySQL-backed PIEAS instead. Same
    injection pattern as ExtractorAgent's `source` parameter; the
    generation logic itself is 100% backend-agnostic.
    """
    insert_student = source.insert_student
    insert_faculty = source.insert_faculty
    insert_course = source.insert_course

    fake = Faker()
    Faker.seed(seed)
    rng = random.Random(seed)

    for i in range(1, num_students + 1):
        dept = rng.choice(DEPARTMENTS)
        batch = rng.choice(BATCH_YEARS)
        code = DEPARTMENT_CODES[dept]
        student = PieasStudent(
            pieas_id=f"PIEAS-STU-{i:05d}",
            roll_number=f"{batch}-{code}-{i:03d}",
            first_name=fake.first_name(),
            last_name=fake.last_name(),
            email=fake.unique.email(),
            gender=rng.choice(["male", "female"]),
            date_of_birth=fake.date_of_birth(minimum_age=18, maximum_age=25),
            department=dept,
            batch_year=batch,
            last_updated=_random_past_timestamp(rng),
        )
        insert_student(conn, student)

    for i in range(1, num_faculty + 1):
        dept = rng.choice(DEPARTMENTS)
        faculty = PieasFaculty(
            pieas_id=f"PIEAS-FAC-{i:05d}",
            employee_code=f"EMP-{i:04d}",
            first_name=fake.first_name(),
            last_name=fake.last_name(),
            email=fake.unique.email(),
            gender=rng.choice(["male", "female"]),
            date_of_birth=fake.date_of_birth(minimum_age=28, maximum_age=65),
            department=dept,
            designation=rng.choice(DESIGNATIONS),
            last_updated=_random_past_timestamp(rng),
        )
        insert_faculty(conn, faculty)

    course_names = [
        "Data Structures & Algorithms", "Operating Systems", "Digital Logic Design",
        "Thermodynamics", "Fluid Mechanics", "Circuit Analysis", "Signals & Systems",
        "Process Engineering", "Materials Science", "Reactor Physics",
        "Linear Algebra", "Differential Equations", "Engineering Management",
        "Quantum Mechanics", "Machine Learning", "Database Systems",
    ]
    for i in range(1, num_courses + 1):
        dept = rng.choice(DEPARTMENTS)
        code = DEPARTMENT_CODES[dept]
        course = PieasCourse(
            pieas_id=f"PIEAS-CRS-{i:05d}",
            code=f"{code}-{100 + i * 10}",
            name=course_names[(i - 1) % len(course_names)],
            department=dept,
            credit_hours=rng.choice([2, 3, 4]),
            semester=rng.choice(["Fall", "Spring"]),
            last_updated=_random_past_timestamp(rng),
        )
        insert_course(conn, course)

    return {"student": num_students, "faculty": num_faculty, "course": num_courses}
