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
"""

# ─────────────────────────────────────────────────────────────
# 1.  CATEGORY KEYWORD MATRICES
#     Keys match DURATION_RULES below.  Order matters: the first
#     category whose keywords match wins, so keep the most
#     specific / highest-priority categories first.
# ─────────────────────────────────────────────────────────────
CATEGORY_MATRICES: dict[str, list[str]] = {

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
# 2.  DURATION RULES (minimum hours per category)
#     "other" is the catch-all fallback for posts that don't
#     match any named category.
# ─────────────────────────────────────────────────────────────
DURATION_RULES: dict[str, float] = {
    "data_science":      3.0,
    "coding":            3.0,
    "ethical_hacking":   3.0,
    "digital_marketing": 6.0,
    "design":            6.0,
    "linguistics":       10.0,
    "other":             8.0,   # raised from 5.0 — uncategorized posts now need > 8h
}

# Badge families from the Udemy API that indicate high demand
POPULAR_BADGES = {"bestseller", "hot_and_new", "highest_rated"}

# Only enroll courses in these languages (ISO 639-1 codes)
# "en" = English,  "hi" = Hindi
ALLOWED_LANGUAGES: set[str] = {"en", "hi"}


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
    if rating < 4.0:
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
