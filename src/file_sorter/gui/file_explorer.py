from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QDir, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFileSystemModel,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)


class FileExplorer(QWidget):
    """A VS Code-style file tree sidebar, backed by :class:`QFileSystemModel`.

    The model only reads a directory's contents once it's actually
    expanded in the view -- nothing below the current root is walked or
    loaded up front, so pointing this at a huge drive costs nothing until
    the user drills into a given folder (via the expand arrow or a
    double-click, both of which trigger the same on-demand fetch).
    """

    directory_chosen = Signal(str)

    def __init__(self, root: Optional[Path] = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        header_row = QHBoxLayout()
        header_label = QLabel("FILES")
        header_label.setStyleSheet("font-weight: 600; letter-spacing: 1px; color: #888;")
        open_btn = QPushButton("Open Folder...")
        open_btn.setFlat(True)
        open_btn.clicked.connect(self._choose_root)
        header_row.addWidget(header_label)
        header_row.addStretch()
        header_row.addWidget(open_btn)
        layout.addLayout(header_row)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search files...")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self._on_search_changed)
        layout.addWidget(self.search_box)

        add_row = QHBoxLayout()
        add_selected_btn = QPushButton("Add Selected to Scan")
        add_selected_btn.clicked.connect(self._add_selected)
        add_row.addStretch()
        add_row.addWidget(add_selected_btn)
        layout.addLayout(add_row)

        self.model = QFileSystemModel(self)
        # `AllDirs` (rather than `Dirs`) would tell Qt to list every
        # directory regardless of name filters, which would make the
        # search box below filter files but not folders.
        self.model.setFilter(QDir.Filter.Dirs | QDir.Filter.Files | QDir.Filter.NoDotAndDotDot)

        self.tree = QTreeView()
        self.tree.setModel(self.model)
        self.tree.setHeaderHidden(True)
        for column in range(1, self.model.columnCount()):
            self.tree.hideColumn(column)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setUniformRowHeights(True)
        self.tree.setAnimated(True)
        self.tree.setExpandsOnDoubleClick(True)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self.tree, 1)

        self.set_root(root or Path.home())

    # -- root / navigation ------------------------------------------------

    def set_root(self, root: Path) -> None:
        index = self.model.setRootPath(str(root))
        self.tree.setRootIndex(index)

    def _choose_root(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Open folder")
        if directory:
            self.set_root(Path(directory))

    # -- search -------------------------------------------------------------

    def _on_search_changed(self, text: str) -> None:
        # Only filters names within directories already expanded/loaded --
        # a true recursive search would mean walking (and thus loading)
        # every directory under the root, which defeats the point of
        # loading lazily in the first place.
        self.model.setNameFilters([f"*{text}*"] if text else [])
        self.model.setNameFilterDisables(False)

    # -- selection / activation --------------------------------------------

    def selected_path(self) -> Optional[Path]:
        indexes = self.tree.selectionModel().selectedRows() if self.tree.selectionModel() else []
        if not indexes:
            return None
        return Path(self.model.filePath(indexes[0]))

    def _add_selected(self) -> None:
        path = self.selected_path()
        if path is not None and path.is_dir():
            self.directory_chosen.emit(str(path))

    def _show_context_menu(self, pos) -> None:
        index = self.tree.indexAt(pos)
        if not index.isValid():
            return
        path = Path(self.model.filePath(index))
        if not path.is_dir():
            return
        menu = QMenu(self)
        add_action = menu.addAction("Add to scan directories")
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen == add_action:
            self.directory_chosen.emit(str(path))
