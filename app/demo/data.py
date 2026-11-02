"""Static, fictitious content for the demo organization (§1.4: nothing here is a real person).

Movable festival dates are the published dates for the years listed; for any other year the
calendar falls back to the fixed-date national holidays only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

DOMAIN = "bharattech.test"
ORGANIZATION = {
    "organization_code": "BTSPL",
    "legal_name": "Bharat Tech Solutions Private Limited",
    "display_name": "Bharat Tech",
    "cin": "U72900KA2018PTC123456",
    "address_line_1": "Tower B, Embassy Tech Village, Outer Ring Road",
    "city": "Bengaluru",
    "state": "Karnataka",
    "state_code": "29",
    "postal_code": "560103",
}

BRANCHES = (
    {
        "branch_code": "BLR01",
        "branch_name": "Bengaluru Head Office",
        "address_line_1": "Tower B, Embassy Tech Village, Outer Ring Road",
        "city": "Bengaluru",
        "state": "Karnataka",
        "postal_code": "560103",
    },
    {
        "branch_code": "MUM01",
        "branch_name": "Mumbai Branch",
        "address_line_1": "Level 5, Business Bay, Andheri East",
        "city": "Mumbai",
        "state": "Maharashtra",
        "postal_code": "400093",
    },
    {
        "branch_code": "GGN01",
        "branch_name": "Gurugram Sales Office",
        "address_line_1": "Sector 44, Golf Course Road",
        "city": "Gurugram",
        "state": "Haryana",
        "postal_code": "122003",
    },
)

DEPARTMENTS = (
    ("ENGG", "Engineering"),
    ("HR", "Human Resources"),
    ("FIN", "Finance & Accounts"),
    ("SALES", "Sales"),
)
DESIGNATIONS = (
    ("EM", "Engineering Manager"),
    ("SSE", "Senior Software Engineer"),
    ("SE", "Software Engineer"),
    ("HRE", "HR Executive"),
    ("SM", "Sales Manager"),
    ("SX", "Sales Executive"),
    ("ACC", "Accountant"),
)
GRADES = (
    (1, "Associate"),
    (2, "Executive"),
    (3, "Senior Executive"),
    (4, "Manager"),
    (5, "Senior Manager"),
)


@dataclass(frozen=True, slots=True)
class Person:
    key: str
    first: str
    last: str
    gender: str
    dob: str
    department: str
    designation: str
    grade: int
    branch: str
    joined_days_ago: int
    reports_to: str | None = None
    marital_status: str = "SINGLE"
    father: str | None = None


ACTIVE_PEOPLE = (
    Person(
        "rajesh",
        "Rajesh",
        "Iyer",
        "MALE",
        "1985-06-14",
        "ENGG",
        "EM",
        5,
        "BLR01",
        1500,
        marital_status="MARRIED",
        father="Venkatesh Iyer",
    ),
    Person(
        "priya",
        "Priya",
        "Nair",
        "FEMALE",
        "1992-02-09",
        "ENGG",
        "SSE",
        3,
        "BLR01",
        900,
        "rajesh",
        "MARRIED",
        "Gopalan Nair",
    ),
    Person(
        "arjun",
        "Arjun",
        "Mehta",
        "MALE",
        "1996-11-23",
        "ENGG",
        "SE",
        2,
        "BLR01",
        420,
        "rajesh",
        father="Dinesh Mehta",
    ),
    Person(
        "kavita",
        "Kavita",
        "Joshi",
        "FEMALE",
        "1994-08-30",
        "ENGG",
        "SE",
        2,
        "BLR01",
        600,
        "rajesh",
        father="Suresh Joshi",
    ),
    Person(
        "sneha",
        "Sneha",
        "Kulkarni",
        "FEMALE",
        "1990-04-18",
        "HR",
        "HRE",
        3,
        "MUM01",
        1100,
        marital_status="MARRIED",
        father="Prakash Kulkarni",
    ),
    Person(
        "ananya",
        "Ananya",
        "Reddy",
        "FEMALE",
        "1993-12-05",
        "FIN",
        "ACC",
        2,
        "MUM01",
        700,
        father="Narayan Reddy",
    ),
    Person(
        "vikram",
        "Vikram",
        "Singh",
        "MALE",
        "1988-09-02",
        "SALES",
        "SM",
        4,
        "GGN01",
        1300,
        marital_status="MARRIED",
        father="Harpal Singh",
    ),
    Person(
        "faizan",
        "Faizan",
        "Ansari",
        "MALE",
        "1997-03-27",
        "SALES",
        "SX",
        1,
        "GGN01",
        300,
        "vikram",
        father="Irfan Ansari",
    ),
)
# Pre-joining hires: employees in DRAFT, each at a different point of onboarding.
JOINERS = (
    Person(
        "rohan",
        "Rohan",
        "Desai",
        "MALE",
        "1999-01-12",
        "ENGG",
        "SE",
        1,
        "BLR01",
        -10,
        "rajesh",
        father="Kishore Desai",
    ),
    Person(
        "divya",
        "Divya",
        "Menon",
        "FEMALE",
        "1998-07-21",
        "FIN",
        "ACC",
        1,
        "MUM01",
        -14,
        father="Sreekumar Menon",
    ),
    Person(
        "aditya",
        "Aditya",
        "Verma",
        "MALE",
        "1995-10-08",
        "SALES",
        "SX",
        1,
        "GGN01",
        -3,
        "vikram",
        father="Rakesh Verma",
    ),
    Person(
        "neha",
        "Neha",
        "Banerjee",
        "FEMALE",
        "2000-05-16",
        "HR",
        "HRE",
        1,
        "MUM01",
        -21,
        father="Subir Banerjee",
    ),
)

FIXED_HOLIDAYS = (
    ((1, 14), "Makar Sankranti / Pongal", "OPTIONAL"),
    ((1, 26), "Republic Day", "MANDATORY"),
    ((4, 14), "Dr. Ambedkar Jayanti", "OPTIONAL"),
    ((5, 1), "Maharashtra Day / May Day", "MANDATORY"),
    ((8, 15), "Independence Day", "MANDATORY"),
    ((10, 2), "Gandhi Jayanti", "MANDATORY"),
    ((11, 1), "Kannada Rajyotsava", "OPTIONAL"),
    ((12, 25), "Christmas", "MANDATORY"),
)
MOVABLE_HOLIDAYS: dict[int, tuple[tuple[date, str, str], ...]] = {
    2025: (
        (date(2025, 3, 14), "Holi", "MANDATORY"),
        (date(2025, 8, 27), "Ganesh Chaturthi", "OPTIONAL"),
        (date(2025, 10, 2), "Dussehra", "MANDATORY"),
        (date(2025, 10, 21), "Diwali (Lakshmi Puja)", "MANDATORY"),
    ),
    2026: (
        (date(2026, 3, 4), "Holi", "MANDATORY"),
        (date(2026, 9, 14), "Ganesh Chaturthi", "OPTIONAL"),
        (date(2026, 10, 20), "Dussehra", "MANDATORY"),
        (date(2026, 11, 8), "Diwali (Lakshmi Puja)", "MANDATORY"),
    ),
    2027: (
        (date(2027, 3, 22), "Holi", "MANDATORY"),
        (date(2027, 9, 4), "Ganesh Chaturthi", "OPTIONAL"),
        (date(2027, 10, 9), "Dussehra", "MANDATORY"),
        (date(2027, 10, 29), "Diwali (Lakshmi Puja)", "MANDATORY"),
    ),
}

# (code, name, category, value type, period, amount) — monthly INR.
SALARY_COMPONENTS = (
    ("BASIC", "Basic Salary", "EARNINGS", "FIXED_AMOUNT", "MONTHLY"),
    ("HRA", "House Rent Allowance", "ALLOWANCE", "FIXED_AMOUNT", "MONTHLY"),
    ("SPECIAL", "Special Allowance", "ALLOWANCE", "FIXED_AMOUNT", "MONTHLY"),
    ("CONVEY", "Conveyance Allowance", "ALLOWANCE", "FIXED_AMOUNT", "MONTHLY"),
    ("ER_PF", "Employer PF Contribution", "EMPLOYER_BENEFIT", "FIXED_AMOUNT", "MONTHLY"),
    (
        "EE_PF",
        "Employee PF Contribution",
        "EMPLOYEE_DEDUCTION_REFERENCE",
        "FIXED_AMOUNT",
        "MONTHLY",
    ),
    ("JOINBON", "Joining Bonus", "OTHER", "FIXED_AMOUNT", "ONE_TIME"),
)
# Components priced as a percentage of Basic (the amounts below for them are ignored).
PERCENT_OF_BASIC = {"HRA": "40"}
# template code -> (name, {component: amount})
SALARY_TEMPLATES = {
    "JUNIOR": (
        "Associate structure",
        {
            "BASIC": "20000",
            "HRA": "8000",
            "SPECIAL": "6000",
            "CONVEY": "1600",
            "ER_PF": "2400",
            "EE_PF": "2400",
        },
    ),
    "MID": (
        "Senior executive structure",
        {
            "BASIC": "45000",
            "HRA": "18000",
            "SPECIAL": "15000",
            "CONVEY": "1600",
            "ER_PF": "5400",
            "EE_PF": "5400",
        },
    ),
    "MGR": (
        "Manager structure",
        {
            "BASIC": "85000",
            "HRA": "34000",
            "SPECIAL": "40000",
            "CONVEY": "1600",
            "ER_PF": "10200",
            "EE_PF": "10200",
        },
    ),
}
