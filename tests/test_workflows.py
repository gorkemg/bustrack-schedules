"""Static guards for the publishing pipeline (overview/git-branching-model.md).

`main` is integration only: nothing may publish from it. Publishing code comes from the `production` or `staging`
pointers, which promote.yml fast-forwards (production only by the owner).
"""

import pathlib
import re
import unittest

WORKFLOWS = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows"


def read(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def push_branches(text: str) -> list[str]:
    match = re.search(r"^  push:\n    branches:\n((?:      - .+\n)+)", text, re.M)
    return [line.strip()[2:].strip() for line in match.group(1).splitlines()] if match else []


class PublishingPipelineTest(unittest.TestCase):
    def test_publication_listens_only_to_pointers(self):
        self.assertEqual(push_branches(read("schedule_update.yml")), ["production", "staging"])

    def test_publication_checks_out_pointer_code(self):
        text = read("schedule_update.yml")
        blocks = re.findall(r"uses: actions/checkout@[^\n]+\n((?:        .+\n)+)", text)
        self.assertEqual(len(blocks), 2)
        for block in blocks:
            self.assertIn("ref: ${{ env.SOURCE_REF }}", block)
        self.assertNotIn("refs/heads/main", text)

    def test_rolling_tag_names_are_unchanged(self):
        text = read("schedule_update.yml")
        for tag in ('UTA_TAG="uta"', 'PVTA_TAG="pvta"', 'UTA_TAG="uta-beta"', 'PVTA_TAG="pvta-beta"'):
            self.assertIn(tag, text)

    def test_promote_is_fast_forward_only_and_owner_gated(self):
        text = read("promote.yml")
        self.assertIn("environment: ${{ inputs.target }}", text)
        self.assertIn('"$TRIGGERING_ACTOR" != "gorkemg"', text)
        self.assertIn("merge-base --is-ancestor", text)
        self.assertNotIn("--force", text)
        self.assertIn("ssh-key: ${{ secrets.PROMOTE_SSH_KEY }}", text)

    def test_pointer_rollback_is_owner_gated_and_tag_bound(self):
        text = read("rollback-pointer.yml")
        self.assertIn('"$TRIGGERING_ACTOR" != "gorkemg"', text)
        self.assertIn("^prod-[0-9]{8}-[0-9]{4}", text)


if __name__ == "__main__":
    unittest.main()
