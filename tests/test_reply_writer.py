import unittest
from types import SimpleNamespace
from unittest import mock

from src.crew import email_crew
from src.crew.email_crew import clean_reply, generate_reply
from src.tasks.email_tasks import create_email_tasks


class GenerateReplyTests(unittest.TestCase):
    def _patch_crew(self, raw):
        crew = mock.Mock()
        crew.kickoff.return_value = SimpleNamespace(raw=raw)
        patcher = mock.patch.object(
            email_crew,
            "create_email_crew",
            return_value=crew,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return crew

    def test_returns_reviewed_reply(self):
        crew = self._patch_crew("Dear Asha,\n\nThank you.\n\nRegards")

        reply = generate_reply("email text")

        self.assertEqual(reply, "Dear Asha,\n\nThank you.\n\nRegards")
        crew.kickoff.assert_called_once_with(
            inputs={"email_content": "email text"}
        )

    def test_cleans_model_formatting(self):
        self._patch_crew("```text\nSubject: Re: Lab\n\nHello Asha.\n```")

        self.assertEqual(generate_reply("email text"), "Hello Asha.")

    def test_empty_reply_raises(self):
        self._patch_crew("   ")

        with self.assertRaises(ValueError):
            generate_reply("email text")

    def test_model_error_propagates(self):
        crew = self._patch_crew("")
        crew.kickoff.side_effect = RuntimeError("rate limited")

        with self.assertRaises(RuntimeError):
            generate_reply("email text")


class CleanReplyTests(unittest.TestCase):
    def test_plain_text_is_unchanged(self):
        self.assertEqual(clean_reply("  Hello.\n\nThanks  "), "Hello.\n\nThanks")

    def test_keeps_subject_word_inside_body(self):
        text = "Hello,\nThe subject: exams is covered."
        self.assertEqual(clean_reply(text), text)

    def test_none(self):
        self.assertEqual(clean_reply(None), "")


class TaskInstructionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        agents = email_crew.create_email_crew().agents
        cls.analyze, cls.draft, cls.review = create_email_tasks(*agents)

    def test_writer_must_not_invent_facts(self):
        text = self.draft.description.lower()

        self.assertIn("do not invent", text)
        self.assertIn("deadlines", text)
        self.assertIn("commitments", text)

    def test_reviewer_checks_accuracy_relevance_tone_grammar(self):
        text = self.review.description.lower()

        for check in ("accuracy", "relevance", "tone", "grammar"):
            self.assertIn(check, text)

        self.assertIn("do not send", text)


if __name__ == "__main__":
    unittest.main()
