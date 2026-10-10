"""Native application directories shared by storage and process locks."""
import os
from pathlib import Path
from .platforms import system


def application_data_dir():
    if system() == 'windows':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'book-ticket'
    return Path.home() / 'Library/Application Support/book-ticket'


def project_data_dir(project):
    if not project or project in {'.', '..'} or any(c in project for c in '\\/:'):
        raise ValueError('Use one project directory name')
    override = os.environ.get('BOOK_TICKET_DATA_DIR')
    root = Path(override).expanduser().resolve() if override else application_data_dir()
    return root / project
