from collections import OrderedDict
from copy import deepcopy
from gettext import gettext as _

from gi.repository import Gtk

from lutris.config import write_game_config
from lutris.database.games import add_game
from lutris.exception_backstops import async_execute
from lutris.game import Game
from lutris.gui.dialogs import ModelessDialog
from lutris.scanners.default_installers import DEFAULT_INSTALLERS
from lutris.scanners.lutris import get_path_cache
from lutris.scanners.tosec import clean_rom_name, guess_platform, search_tosec_by_md5
from lutris.services.lutris import download_lutris_media_async
from lutris.util.jobs import call_async
from lutris.util.log import logger
from lutris.util.strings import gtk_safe, slugify
from lutris.util.system import get_md5_hash, get_md5_in_zip


class ImportGameDialog(ModelessDialog):
    def __init__(self, files, parent=None) -> None:
        super().__init__(
            _("Import a game"),
            parent=parent,
            border_width=10
        )
        self.files = files
        self.progress_labels = {}
        self.checksum_labels = {}
        self.description_labels = {}
        self.category_labels = {}
        self.error_labels = {}
        self.launch_buttons = {}
        self.platform = None
        self.set_size_request(500, 560)

        self.accelerators = Gtk.AccelGroup()
        self.add_accel_group(self.accelerators)

        scrolledwindow = Gtk.ScrolledWindow(child=self.get_file_labels_listbox(files))
        scrolledwindow.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        frame = Gtk.Frame(
            shadow_type=Gtk.ShadowType.ETCHED_IN,
            child=scrolledwindow)
        self.get_content_area().pack_start(frame, True, True, 6)

        self.close_button = self.add_button(Gtk.STOCK_STOP, Gtk.ResponseType.CANCEL)
        key, mod = Gtk.accelerator_parse("Escape")
        self.close_button.add_accelerator("clicked", self.accelerators, key, mod, Gtk.AccelFlags.VISIBLE)

        self.show_all()
        self.search_task = async_execute(self.search_checksums_async())

    def on_response(self, dialog, response: Gtk.ResponseType) -> None:
        if response in (Gtk.ResponseType.CLOSE, Gtk.ResponseType.CANCEL, Gtk.ResponseType.DELETE_EVENT):
            if self.search_task:
                self.search_task.cancel()
                return  # don't actually close the dialog

        super().on_response(dialog, response)

    def get_file_labels_listbox(self, files):
        listbox = Gtk.ListBox(vexpand=True)
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        for file_path in files:
            row = Gtk.ListBoxRow()
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            hbox.set_margin_left(12)
            hbox.set_margin_right(12)

            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

            description_label = Gtk.Label(halign=Gtk.Align.START)
            vbox.pack_start(description_label, True, True, 5)
            self.description_labels[file_path] = description_label

            file_path_label = Gtk.Label(file_path, halign=Gtk.Align.START, xalign=0)
            file_path_label.set_line_wrap(True)
            vbox.pack_start(file_path_label, True, True, 5)

            progress_label = Gtk.Label(halign=Gtk.Align.START)
            vbox.pack_start(progress_label, True, True, 5)
            self.progress_labels[file_path] = progress_label

            checksum_label = Gtk.Label(no_show_all=True, halign=Gtk.Align.START)
            vbox.pack_start(checksum_label, True, True, 5)
            self.checksum_labels[file_path] = checksum_label

            category_label = Gtk.Label(no_show_all=True, halign=Gtk.Align.START)
            vbox.pack_start(category_label, True, True, 5)
            self.category_labels[file_path] = category_label

            error_label = Gtk.Label(no_show_all=True, halign=Gtk.Align.START, xalign=0)
            error_label.set_line_wrap(True)
            vbox.pack_start(error_label, True, True, 5)
            self.error_labels[file_path] = error_label

            hbox.pack_start(vbox, True, True, 0)

            launch_button = Gtk.Button(_("Launch"), valign=Gtk.Align.CENTER, sensitive=False)
            hbox.pack_end(launch_button, False, False, 0)
            self.launch_buttons[file_path] = launch_button

            row.add(hbox)
            listbox.add(row)
        return listbox

    @property
    def search_stopping(self):
        return self.search_task and self.search_task.cancelled()

    async def search_checksums_async(self):
        game_path_cache = get_path_cache()

        def show_progress(filepath, message):
            self.progress_labels[filepath].set_markup("<i>%s</i>" % gtk_safe(message))

        def get_existing_game(filepath):
            for game_id, game_path in game_path_cache.items():
                if game_path == filepath:
                    return Game(game_id)

            return None

        def get_md5(filepath):
            if filepath.casefold().endswith(".zip"):
                return get_md5_in_zip(filepath)

            return get_md5_hash(filepath)

        async def search_single_async(filepath):
            existing_game = await call_async(get_existing_game, filepath)
            if existing_game:
                # Found a game to launch instead of installing, but we can't safely
                # do this on this thread, so we return the game and handle it later.
                return [{"name": existing_game.name, "game": existing_game, "roms": []}]

            show_progress(filepath, _("Calculating checksum..."))
            md5 = await call_async(get_md5, filepath)

            show_progress(filepath, _("Looking up checksum on Lutris.net..."))
            result = await call_async(search_tosec_by_md5, md5)
            if not result:
                raise RuntimeError(_("This ROM could not be identified."))
            return result

        async def search_async():
            results = OrderedDict()  # must preserve order, on any Python version
            for filepath in self.files:
                if self.search_stopping:
                    break

                try:
                    show_progress(filepath, _("Looking for installed game..."))
                    result = await search_single_async(filepath)
                except Exception as error:
                    result = [{"error": error, "roms": []}]
                finally:
                    show_progress(filepath, "")

                if result:
                    results[filepath] = result
            return results

        try:
            results = await search_async()
        finally:
            self.search_task = None
            self.close_button.set_label(Gtk.STOCK_CLOSE)

        for filename, result in results.items():
            for rom_set in result:
                if await self.import_rom_async(rom_set, filename):
                    break

    async def import_rom_async(self, rom_set, filename):
        """Tries to install a specific ROM, or reports failure. Returns True if
        successful, False if not."""
        try:
            self.progress_labels[filename].hide()

            if "error" in rom_set:
                raise rom_set["error"]

            if "game" in rom_set:
                game = rom_set["game"]
                self.display_existing_game_info(filename, game)
                self.enable_game_launch(filename, game)
                return True

            for rom in rom_set["roms"]:
                self.display_new_game_info(filename, rom_set, rom["md5"])
                game_id = await self.add_game_async(rom_set, filename)
                game = Game(game_id)
                game.emit("game-installed")
                game.emit("game-updated")
                self.enable_game_launch(filename, game)
                return True
        except Exception as ex:
            logger.exception(_("Failed to import a ROM: %s"), ex)
            error_label = self.error_labels[filename]
            error_label.set_markup(
                "<span style=\"italic\" foreground=\"red\">%s</span>" % gtk_safe(str(ex)))
            error_label.show()

        return False

    def enable_game_launch(self, filename, game):
        launch_button = self.launch_buttons[filename]
        launch_button.set_sensitive(True)
        launch_button.connect("clicked", self.on_launch_clicked, game)

    def on_launch_clicked(self, _button, game):
        game.emit("game-launch")
        self.destroy()

    def display_existing_game_info(self, filename, game):
        label = self.checksum_labels[filename]
        label.set_markup("<i>%s</i>" % _("Game already installed in Lutris"))
        label.show()
        label = self.description_labels[filename]
        label.set_markup("<b>%s</b>" % game.name)
        category = game.platform
        label = self.category_labels[filename]
        label.set_text(category)
        label.show()

    def display_new_game_info(self, filename, rom_set, checksum):
        label = self.checksum_labels[filename]
        label.set_text(checksum)
        label.show()
        label = self.description_labels[filename]
        label.set_markup("<b>%s</b>" % rom_set["name"])
        category = rom_set["category"]["name"]
        label = self.category_labels[filename]
        label.set_text(category)
        label.show()
        self.platform = guess_platform(rom_set)

        if not self.platform:
            raise RuntimeError(_("The platform '%s' is unknown to Lutris.") % category)

    async def add_game_async(self, rom_set, filepath):
        name = clean_rom_name(rom_set["name"])
        logger.info("Installing %s", name)

        try:
            installer = deepcopy(DEFAULT_INSTALLERS[self.platform])
        except KeyError as error:
            raise RuntimeError(
                _("Lutris does not have a default installer for the '%s' platform.") % self.platform) from error

        for key, value in installer["game"].items():
            if value == "rom":
                installer["game"][key] = filepath
        slug = slugify(name)
        configpath = write_game_config(slug, installer)
        game_id = add_game(
            name=name,
            runner=installer["runner"],
            slug=slug,
            directory="",
            installed=1,
            installer_slug="%s-%s" % (slug, installer["runner"]),
            configpath=configpath
        )
        await download_lutris_media_async(slug)
        return game_id
