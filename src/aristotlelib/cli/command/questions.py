import argparse
import contextlib
import logging

from aristotlelib.agent_task import AgentTask
from aristotlelib.api_request import AristotleAPIError, before_sending
from aristotlelib.cli.api_action import PROJECT_REFERENCE_HELP, APIAction, exit_code_for, print_json
from aristotlelib.cli.question_flow import (
    DEFAULT_DEADLINE_SECONDS,
    can_prompt,
    one_line,
    read_stdin_line,
    run_question_flow,
)
from aristotlelib.date_utils import format_relative_time
from aristotlelib.exit_codes import EXIT_NOT_FOUND, EXIT_OUTCOME_UNKNOWN
from aristotlelib.printable import print_line
from aristotlelib.project import Project, same_project_id
from aristotlelib.question import NOT_ANSWERED, Question, QuestionClosed
from aristotlelib.results import TaskQuestions

QUESTION_WIDTH = 50
ANSWER_WIDTH = 30


def answer_argument(value: str) -> tuple[str, str]:
    """argparse type for ``QUESTION_ID=ANSWER``; returns the id and the answer without surrounding whitespace."""
    question_id, separator, answer = value.partition("=")
    if not separator or not question_id.strip():
        raise argparse.ArgumentTypeError(f"expected QUESTION_ID=ANSWER, got {value!r}")
    if not answer.strip():
        raise argparse.ArgumentTypeError(f"the answer to {question_id.strip()} is empty")
    return question_id.strip(), answer.strip()


def _unanswered(count: int) -> str:
    """What to do about ``count`` answers that a failed request left unsent."""
    return NOT_ANSWERED if count == 1 else "No question was answered, so it is safe to answer them again."


class QuestionsAction(APIAction):
    @property
    def command_name(self) -> str:
        return "questions"

    @property
    def description(self) -> str:
        return "List the questions Aristotle asked during a task, and answer the pending ones."

    @classmethod
    def priority(cls) -> int:
        return 75

    def add_action_arguments(self) -> None:
        self.parser.add_argument(
            "project_id",
            type=str,
            help=f"The project whose questions to show: {PROJECT_REFERENCE_HELP}",
        )
        self.parser.add_argument(
            "--task",
            type=str,
            dest="task_id",
            help="A specific task instead of the most recent one",
        )
        self.parser.add_argument(
            "--json",
            action="store_true",
            help='Print {"project_id", "task", "questions"} as JSON instead of a table, and never prompt',
        )
        self.parser.add_argument(
            "--answer",
            action="append",
            type=answer_argument,
            metavar="QUESTION_ID=ANSWER",
            help=(
                "Answer a pending question without prompting; repeat it to answer several. The ids are in the "
                "--json output. Nothing is sent unless every id names a question that is still open."
            ),
        )

    def preflight(self, args: argparse.Namespace) -> int | None:
        ids = [question_id for question_id, _ in args.answer or []]
        repeated = sorted({question_id for question_id in ids if ids.count(question_id) > 1})
        if repeated:
            self.parser.error(f"Each question takes one answer; {', '.join(repeated)} has more than one.")
        return None

    async def run_action(self, args: argparse.Namespace) -> int | None:
        with before_sending(_unanswered(len(args.answer))) if args.answer else contextlib.nullcontext():
            args.project_id, listed = await Project._resolve(args.project_id)
            task = await self._find_task(args, listed)
            if task is None:
                return EXIT_NOT_FOUND if args.answer else None

            # As on the web, an open question is awaiting an answer only while its run is live.
            questions = await Question.list_for_task(task.agent_task_id, run_live=task.live)

        if args.answer:
            return await self._answer(args, task, questions)

        if args.json:
            print_json(TaskQuestions(project_id=task.project_id, task=task, questions=questions))
            return None

        if not questions:
            logging.info("Aristotle has not asked any questions on this task.")
            return None

        live = task.live
        print(f"{'STATUS':<11} {'ASKED':<20} {'QUESTION':<{QUESTION_WIDTH}} {'ANSWER':<{ANSWER_WIDTH}}")
        print("-" * (11 + 20 + QUESTION_WIDTH + ANSWER_WIDTH + 3))
        for question in questions:
            asked = format_relative_time(question.created_at)
            content = one_line(question.content, QUESTION_WIDTH)
            answer = one_line(question.response, ANSWER_WIDTH) if question.response else "-"
            print_line(f"{question.state:<11} {asked:<20} {content:<{QUESTION_WIDTH}} {answer:<{ANSWER_WIDTH}}")

        pending = [question for question in questions if question.pending]
        if not pending:
            return None
        if not live:
            logging.info("The run has ended, so no answer can reach Aristotle now.")
            return None
        if task.read_only:
            logging.info("Only the project's owner can answer these.")
            return None

        count = len(pending)
        noun = "question" if count == 1 else "questions"
        if not can_prompt():
            # Not an interactive terminal: report the count and do not prompt.
            logging.info(
                f"{count} {noun} pending. Answer at a terminal with: aristotle questions {args.project_id}\n"
                f"or from a script with: aristotle questions {args.project_id} --answer <question-id>=<answer>"
            )
            return None

        print(f"{count} {noun} pending. Answer {'it' if count == 1 else 'them'} now? [Y/n]")
        yes: tuple[str, ...] = ("", "y", "yes")
        while True:
            try:
                # Read off the event loop, so Ctrl-C interrupts it.
                reply = (await read_stdin_line()).strip().lower()
            except EOFError:
                print()
                return None
            if reply in ("n", "no"):
                # Also what someone who answered a yes-or-no question with "no" sees.
                print(f"Nothing was sent. Answer later with: aristotle questions {args.project_id}")
                return None
            if reply in yes:
                break
            # Such as an answer typed straight away, which goes nowhere: say so, rather than drop it silently or start
            # answering on a "no" spelled another way. From now on only y starts, so that a blank line in a pasted
            # answer doesn't.
            print("Nothing was sent. Type y to answer, n to leave it.")
            yes = ("y", "yes")

        await run_question_flow(
            pending,
            read_line=read_stdin_line,
            write=print_line,
            deadline_seconds=DEFAULT_DEADLINE_SECONDS,
            run_is_live=task.refresh_live,
        )
        return None

    @staticmethod
    async def _answer(args: argparse.Namespace, task: AgentTask, questions: list[Question]) -> int:
        """Send each ``--answer``; nothing is sent unless every id names an open question of the task."""
        ids = [question_id for question_id, _ in args.answer]
        by_id = {question.question_id: question for question in questions}
        unknown = [question_id for question_id in ids if question_id not in by_id]
        if unknown:
            logging.error(
                f"Task {task.agent_task_id} has no question {', '.join(unknown)}. See its questions with:\n"
                f"  aristotle questions {task.project_id} --task {task.agent_task_id} --json"
            )
            return EXIT_NOT_FOUND
        if not task.live:
            logging.error("The run has ended, so no answer can reach Aristotle now. Nothing was sent.")
            return EXIT_NOT_FOUND
        if task.read_only:
            logging.error("Only the project's owner can answer these. Nothing was sent.")
            return EXIT_NOT_FOUND
        closed = [question_id for question_id in ids if not by_id[question_id].pending]
        if closed:
            logging.error(f"Question {', '.join(closed)} is already closed. Nothing was sent.")
            return EXIT_NOT_FOUND

        answered: list[Question] = []
        exit_code = None
        for question_id, answer in args.answer:
            question = by_id[question_id]
            try:
                await question.answer(answer)
            except QuestionClosed as e:
                logging.error(f"Question {question_id}: {e}")
                exit_code = exit_code or EXIT_NOT_FOUND
                continue
            except AristotleAPIError as e:
                # The remaining answers are still sent. The exit code is the first failure's, or 7 if any answer's
                # outcome is unknown.
                logging.error(f"Question {question_id} could not be answered: {e}")
                code = exit_code_for(e)
                exit_code = code if code == EXIT_OUTCOME_UNKNOWN else exit_code or code
                continue
            logging.info(f"Answered question {question_id}: {answer}")
            answered.append(question)

        if args.json:
            print_json(TaskQuestions(project_id=task.project_id, task=task, questions=answered))
        return exit_code or 0

    async def _find_task(self, args: argparse.Namespace, listed: Project | None) -> AgentTask | None:
        if args.task_id is not None:
            task = await AgentTask.from_id(args.task_id)
            if not same_project_id(task.project_id, args.project_id):
                logging.error(
                    f"Task {args.task_id} belongs to project {task.project_id}, but project {args.project_id} provided."
                )
                raise SystemExit(EXIT_NOT_FOUND)
            return task

        project = listed if listed is not None else await Project.from_id(args.project_id)
        tasks, _ = await project.get_tasks(limit=1)
        if not tasks:
            logging.info("No tasks found for this project.")
            if args.json:
                # Under --json a project with no tasks has no questions, rather than empty output.
                print_json(TaskQuestions(project_id=args.project_id, task=None, questions=[]))
            return None
        return tasks[0]
