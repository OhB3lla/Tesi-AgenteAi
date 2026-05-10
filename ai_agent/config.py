import re


BYPASS_ENV_VAR = "AI_AGENT_BYPASS"
BYPASS_TTL_SECONDS = 120
DIFF_CONTEXT_LINES = 8
API_KEY_FALLBACK_FILE = ".api_key"
SOURCE_EXTENSIONS = frozenset((".py", ".dart", ".swift", ".js", ".ts", ".java", ".cpp", ".c", ".cs"))
LANGUAGE_NAMES = {
    ".py": "Python",
    ".js": "JavaScript",
    ".ts": "TypeScript",
    ".dart": "Dart",
    ".swift": "Swift",
    ".java": "Java",
    ".cpp": "C++",
    ".c": "C",
    ".cs": "C#",
}
MAX_FILE_SIZE_BYTES = 150 * 1024
MAX_FILE_LINES = 2000
MAX_RETRY_ATTEMPTS = 3
MAX_FILES_TO_ANALYZE = 10
TEST_TIMEOUT_SECONDS = 30
API_TIMEOUT_SECONDS = 45
RETRY_DELAYS_SECONDS = [5, 15, 30]

ZERO_SHA_RE = re.compile(r"^0+$")
