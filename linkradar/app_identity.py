"""Public product identity; service IDs/data paths stay compatible with upgrades."""
from pathlib import Path

BRANDING_DIR = Path(__file__).resolve().with_name("branding")

NAME = 'LinkRadar'
PROJECT_URL = 'https://github.com/mshnyrev13/linkradar-updates'
GUIDE_URL = 'https://wiki.yandex-team.ru/hd/vcs/vcs-for-help/linkradar/'
PUBLIC_GUIDE_URL = PROJECT_URL + '/blob/main/README.md'
