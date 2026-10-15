"""SDK demo: submit a draft and block until a human decides.

Terminal 1:  draftgate serve
Terminal 2:  python examples/sdk_demo.py
Browser:     open http://127.0.0.1:8010 and make a decision.
"""

from draftgate import Client

gate = Client("http://127.0.0.1:8010")

print("submitting draft, waiting for human decision (timeout 10 min)...")
result = gate.review(
    title="Weekly AI news digest",
    content=(
        "# This week in AI\n\n"
        "1. Something happened\n"
        "2. Something else happened\n\n"
        "Edit me before approving, if you like."
    ),
    content_type="markdown",
    tags=["newsletter", "demo"],
    metadata={"source": "sdk_demo", "pipeline": "example"},
    timeout=600,
)

print("status:      ", result["status"])
print("final output:", (result.get("final_content") or result["content"])[:80], "...")
if result.get("reason"):
    print("reason:      ", result["reason"])  # reviewer's note, sent on reject
