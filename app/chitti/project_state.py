from pathlib import Path

FILES = ("plan.md", "tasks.md", "architecture.md", "open_questions.md")


class ProjectState:
    def __init__(self, project_root: str) -> None:
        self.project_root = Path(project_root).resolve()

    def directory(self, project: str) -> Path:
        candidate = (self.project_root / project / ".chitti").resolve()
        if self.project_root not in candidate.parents:
            raise ValueError("project escapes PROJECT_ROOT")
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate

    def read(self, project: str) -> dict[str, str]:
        directory = self.directory(project)
        return {
            name: (directory / name).read_text() if (directory / name).exists() else ""
            for name in FILES
        }

    def write(self, project: str, name: str, content: str) -> None:
        if name not in FILES:
            raise ValueError(f"unsupported project-state file: {name}")
        self.directory(project).joinpath(name).write_text(content)
