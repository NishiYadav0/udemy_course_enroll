"""
utils/filter.py
---------------
ScholarSync — Layer 1 & Layer 4 of the pipeline.

Layer 1 : keyword_match()
    Scans raw Telegram post text to decide which category the post belongs to.
    Returns (True, category_name) even for uncategorised posts so the pipeline
    can apply the "other" duration rule instead of silently dropping them.

Layer 4 : evaluate_course_policy()
    Applies all guardrails and per-category duration rules to live course
    metadata fetched from the Udemy API. Returns True only when the course
    should be auto-enrolled.

CATEGORY_MATRICES / DURATION_RULES are admin-editable (2026-08-25) via the
panel's Categories page, which writes config/category_policy.json. The
dicts below (_DEFAULT_*) are the built-in fallback this project has always
shipped with — used as-is on a fresh deploy where nobody has customised
anything yet, AND as the safety net if the JSON file is ever missing or
malformed, so a bad edit can never take category matching down. See
_load_policy() below for exactly how the two are merged.
"""

import json
import os

# ─────────────────────────────────────────────────────────────
# 1.  CATEGORY KEYWORD MATRICES (built-in defaults)
#     Keys match DURATION_RULES below.  Order matters: the first
#     category whose keywords match wins, so keep the most
#     specific / highest-priority categories first.
# ─────────────────────────────────────────────────────────────
_DEFAULT_CATEGORY_MATRICES: dict[str, list[str]] = {

    # ── Primary track: IIT Madras BS Data Science curriculum ──
    "data_science": [
        "data science", "machine learning", "deep learning", "nlp",
        "natural language processing", "pandas", "numpy", "matplotlib",
        "seaborn", "scikit", "sklearn", "tensorflow", "keras", "pytorch",
        "statistics", "statistical", "probability", "calculus",
        "linear algebra", "regression", "classification", "clustering",
        "neural network", "computer vision", "time series", "forecasting",
        "tableau", "power bi", "duckdb", "data analysis", "data analyst",
        "data engineer", "data pipeline", "data visualization",
        "business intelligence", "bi dashboard", "spark", "hadoop",
        "big data", "feature engineering", "model deployment", "mlops",
        "algorithms", "data structures", "dsa", "reinforcement learning",
        "generative ai", "llm", "large language model", "transformer",
        "bert", "gpt", "fine tuning", "rag", "vector database",
        "langchain", "chatgpt api", "openai api", "hugging face",
        "stable diffusion", "midjourney", "prompt engineering",
        "claude", "gemini", "ai agent", "artificial intelligence",
        "copilot", "github copilot", "cursor ai",
    ],

    # ── Coding & software development ─────────────────────────
    "coding": [
        "python", "sql", "mysql", "postgresql", "sqlite", "mongodb",
        "javascript", "typescript", "react", "next.js", "vue", "angular",
        "node.js", "express", "java", "spring boot", "kotlin",
        "c++", "c#", ".net", "rust", "go", "golang",
        "web development", "full stack", "frontend", "backend",
        "html", "css", "tailwind", "bootstrap",
        "rest api", "graphql", "microservices", "api development",
        "flask", "fastapi", "django", "ruby on rails",
        "git", "github", "docker", "kubernetes", "devops", "ci/cd",
        "linux", "bash", "shell scripting", "aws", "azure", "gcp",
        "cloud computing", "terraform", "ansible",
        "mobile app", "android", "ios", "flutter", "react native",
        "excel", "vba", "power query", "power automate",
    ],

    # ── Ethical hacking & cybersecurity ───────────────────────
    "ethical_hacking": [
        "ethical hacking", "hacking", "cybersecurity", "cyber security",
        "penetration testing", "pen test", "pentest",
        "kali linux", "metasploit", "nmap", "wireshark", "burp suite",
        "bug bounty", "oscp", "ceh", "comptia security",
        "network security", "web security", "application security",
        "vulnerability assessment", "exploit", "ctf",
        "digital forensics", "incident response", "siem",
        "social engineering", "phishing", "malware analysis",
    ],

    # ── Digital marketing ──────────────────────────────────────
    "digital_marketing": [
        "digital marketing", "seo", "search engine optimization",
        "sem", "search engine marketing", "google ads", "facebook ads",
        "instagram marketing", "social media marketing",
        "content marketing", "email marketing", "affiliate marketing",
        "influencer marketing", "youtube marketing",
        "growth hacking", "conversion rate", "cro", "a/b testing",
        "google analytics", "meta ads", "tiktok ads",
        "dropshipping", "e-commerce marketing", "shopify marketing",
        "copywriting", "sales funnel", "lead generation",
    ],

    # ── Design & creative editing ──────────────────────────────
    "design": [
        "graphic design", "ui ux", "ui/ux", "user interface",
        "user experience", "product design", "web design",
        "figma", "adobe xd", "sketch",
        "photoshop", "illustrator", "indesign", "adobe creative",
        "canva", "logo design", "branding", "typography",
        "motion graphics", "after effects",
        "video editing", "premiere pro", "davinci resolve",
        "final cut pro", "capcut", "filmora",
        "3d design", "blender", "maya", "cinema 4d",
        "photo editing", "lightroom",
    ],

    # ── Linguistics & communication ───────────────────────────
    "linguistics": [
        "english speaking", "spoken english", "english communication",
        "public speaking", "communication skills", "presentation skills",
        "ielts", "toefl", "pte", "gre verbal",
        "language learning", "french", "german", "spanish", "japanese",
        "grammar", "writing skills", "business english",
        "personality development", "soft skills",
    ],
}

# ─────────────────────────────────────────────────────────────
# 2.  DURATION RULES (minimum hours per category, built-in defaults)
#     "other" is the catch-all fallback for posts that don't
#     match any named category.
# ─────────────────────────────────────────────────────────────
_DEFAULT_DURATION_RULES: dict[str, float] = {
    "data_science":      3.0,
    "coding":            3.0,
    "ethical_hacking":   3.0,
    "digital_marketing": 6.0,
    "design":            6.0,
    "linguistics":       10.0,
    "other":             8.0,   # raised from 5.0 — uncategorized posts now need > 8h
}

# ─────────────────────────────────────────────────────────────
# 2b.  LOAD ADMIN OVERRIDES, IF ANY
#      config/category_policy.json is written by the admin panel's
#      Categories page (admin_panel/category_editor.py). Never required —
#      a missing or broken file just means "use the built-in defaults",
#      exactly like this project behaved before this file existed.
# ─────────────────────────────────────────────────────────────
def _load_policy() -> tuple[dict[str, list[str]], dict[str, float]]:
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    policy_path = os.path.join(project_root, "config", "category_policy.json")

    if not os.path.exists(policy_path):
        return dict(_DEFAULT_CATEGORY_MATRICES), dict(_DEFAULT_DURATION_RULES)

    try:
        with open(policy_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)

        matrices: dict[str, list[str]] = {}
        rules: dict[str, float] = {}
        for cat in data["categories"]:
            name = cat["name"]
            matrices[name] = [str(k).strip().lower() for k in cat.get("keywords", []) if str(k).strip()]
            rules[name] = float(cat.get("min_hours", 8.0))

        if not matrices:
            raise ValueError("category_policy.json has zero categories")

        rules["other"] = float(data.get("other_min_hours", 8.0))
        return matrices, rules

    except Exception as exc:
        # A malformed edit must NEVER take the whole bot down — fall back
        # to the safe built-in defaults and keep running. This prints once
        # at import time (goes to the same log the rest of the bot uses)
        # so the problem is visible without silently mis-enrolling courses.
        print(f"[filter.py] WARNING: couldn't load {policy_path} ({exc}) — "
              f"using built-in default categories instead.")
        return dict(_DEFAULT_CATEGORY_MATRICES), dict(_DEFAULT_DURATION_RULES)


CATEGORY_MATRICES, DURATION_RULES = _load_policy()

# Badge families from the Udemy API that indicate high demand
POPULAR_BADGES = {"bestseller", "hot_and_new", "highest_rated"}

# Only enroll courses in these languages (ISO 639-1 codes)
# "en" = English,  "hi" = Hindi
ALLOWED_LANGUAGES: set[str] = {"en", "hi"}

# Minimum star rating for a course that HAS been rated.
MIN_RATING: float = 4.0

# Brand-new courses have rating == 0.0 because nobody has reviewed them yet —
# that is "unrated", NOT "badly rated". Free 100%-off coupons are overwhelmingly
# used by instructors to launch brand-new courses, so rejecting rating==0 threw
# away almost every genuine target. Set this to False to go back to the old
# (much stricter) behaviour.
ALLOW_UNRATED: bool = True

# Practice-test / question-bank courses report estimated_content_length == 0
# because they contain no video at all — the duration rule can never be
# satisfied by them. When True, a 0-minute course is judged on its other
# guardrails (language, paid, coupon, rating) and exempted from the duration
# minimum instead of being dropped outright.
ALLOW_ZERO_DURATION_PRACTICE_TESTS: bool = True


# ─────────────────────────────────────────────────────────────
# 3.  LAYER 1 — KEYWORD MATCH
# ─────────────────────────────────────────────────────────────
def keyword_match(text: str) -> tuple[bool, str]:
    """
    Scan post text against all category keyword lists.

    Returns
    -------
    (True,  category_name) – if a keyword match is found
    (True,  "other")       – if no specific category matched but the post
                             still contains a URL (handled downstream with
                             the "other" duration rule)
    (False, "")            – if the post has no text worth processing
                             (caller should drop immediately)
    """
    if not text or len(text.strip()) < 10:
        return False, ""

    lower = text.lower()

    for category, keywords in CATEGORY_MATRICES.items():
        if any(kw in lower for kw in keywords):
            return True, category

    # No named category matched — fall through to "other" bucket
    # so we still check duration for any Udemy link that appears
    return True, "other"


# ─────────────────────────────────────────────────────────────
# 4.  LAYER 4 — COURSE POLICY ENGINE
# ─────────────────────────────────────────────────────────────
def evaluate_course_policy(
    title: str,
    duration_hours: float,
    rating: float,
    is_paid: bool,
    price: float,
    badges: list[dict],
    category: str,
    language: str = "en",
) -> tuple[bool, str]:
    """
    Apply all guardrails and duration rules to a specific course.

    Parameters
    ----------
    title           : Course title string from Udemy API
    duration_hours  : estimated_content_length in minutes ÷ 60
    rating          : Udemy star rating (0.0 – 5.0)
    is_paid         : True if the course is originally a paid course
    price           : Current price after coupon applied (should be 0.0)
    badges          : List of badge dicts from Udemy API
    category        : Category string returned by keyword_match()
    language        : ISO 639-1 code from Udemy locale ("en", "hi", etc.)

    Returns
    -------
    (True,  "reason")  – enroll this course
    (False, "reason")  – drop this course, with reason for logging
    """
    title_display = title[:60] if title else "Unknown"

    # ── GUARDRAIL 0: Language filter ─────────────────────────
    # Only enroll English or Hindi courses
    if language not in ALLOWED_LANGUAGES:
        return False, (
            f"DROPPED | Language '{language}' (only English/Hindi allowed) | {title_display}"
        )

    # ── GUARDRAIL 1: Must be a paid course ───────────────────
    if not is_paid:
        return False, f"DROPPED | Natively free on Udemy | {title_display}"

    # ── GUARDRAIL 2: Coupon must bring price to zero ──────────
    if price is not None and price > 0.50:
        return False, f"DROPPED | Coupon expired, price=${price:.2f} | {title_display}"

    # ── GUARDRAIL 3: Minimum rating ───────────────────────────
    # rating == 0.0 means "no reviews yet", not "rated badly". Brand-new
    # courses are exactly what free coupons promote, so treat 0 separately.
    if rating <= 0.0:
        if not ALLOW_UNRATED:
            return False, f"DROPPED | Unrated (new course) | {title_display}"
        # unrated: allowed through, other guardrails still apply
    elif rating < MIN_RATING:
        return False, f"DROPPED | Low rating {rating:.1f}/5 | {title_display}"

    # ── POPULARITY OVERRIDE: Bestseller / Hot & New ───────────
    is_popular = False
    if badges:
        badge_families = {b.get("badge_family", "").lower() for b in badges}
        if badge_families & POPULAR_BADGES:
            is_popular = True

    if is_popular:
        return True, f"ENROLLED | Popular badge override | {title_display}"

    # ── DURATION RULE: Per-category minimum ──────────────────
    # Practice-test / question-bank courses have no video, so Udemy reports
    # estimated_content_length == 0. The duration minimum is meaningless for
    # them and would drop 100% of them, so exempt that case explicitly.
    if duration_hours <= 0.0:
        if ALLOW_ZERO_DURATION_PRACTICE_TESTS:
            return True, (
                f"ENROLLED | Practice test / no video content "
                f"(duration rule N/A) | {title_display}"
            )
        return False, f"DROPPED | No video content (0.0h) | {title_display}"

    min_hours = DURATION_RULES.get(category, DURATION_RULES["other"])

    if duration_hours >= min_hours:
        return True, (
            f"ENROLLED | {category.upper()} {duration_hours:.1f}h "
            f"(min {min_hours}h) | {title_display}"
        )
    else:
        return False, (
            f"DROPPED | Too short {duration_hours:.1f}h "
            f"(need {min_hours}h for {category}) | {title_display}"
        )
