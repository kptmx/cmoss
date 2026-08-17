from cmoss.bootstrap import bootstrap

bootstrap()

import flet as ft  # noqa: E402

from cmoss.main import main  # noqa: E402

ft.run(main=main)
