"""Tray icon (StatusNotifierItem) published over raw D-Bus.

The GNOME runtime ships no AppIndicator typelib, so the item and its
com.canonical.dbusmenu menu are exported by hand -- the same reason
core/goa.py talks to GNOME Online Accounts over Gio.DBusProxy.

The item is registered by object path, not by a well-known bus name, so the
Flatpak needs only --talk-name=org.kde.StatusNotifierWatcher and no
--own-name. Hosts with no watcher (plain GNOME) simply never call back.
"""

import logging
from collections.abc import Sequence

from gi.repository import Gio, GLib

logger = logging.getLogger(__name__)

ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
WATCHER_NAME = "org.kde.StatusNotifierWatcher"
MENU_INTERFACE = "com.canonical.dbusmenu"

_XML = """
<node>
  <interface name='org.kde.StatusNotifierItem'>
    <property name='Category' type='s' access='read'/>
    <property name='Id' type='s' access='read'/>
    <property name='Title' type='s' access='read'/>
    <property name='Status' type='s' access='read'/>
    <property name='IconName' type='s' access='read'/>
    <property name='ToolTip' type='(sa(iiay)ss)' access='read'/>
    <property name='ItemIsMenu' type='b' access='read'/>
    <property name='Menu' type='o' access='read'/>
    <method name='Activate'>
      <arg type='i' direction='in'/>
      <arg type='i' direction='in'/>
    </method>
  </interface>
  <interface name='com.canonical.dbusmenu'>
    <property name='Version' type='u' access='read'/>
    <property name='Status' type='s' access='read'/>
    <method name='GetLayout'>
      <arg type='i' direction='in'/>
      <arg type='i' direction='in'/>
      <arg type='as' direction='in'/>
      <arg type='u' direction='out'/>
      <arg type='(ia{sv}av)' direction='out'/>
    </method>
    <method name='GetGroupProperties'>
      <arg type='ai' direction='in'/>
      <arg type='as' direction='in'/>
      <arg type='a(ia{sv})' direction='out'/>
    </method>
    <method name='AboutToShow'>
      <arg type='i' direction='in'/>
      <arg type='b' direction='out'/>
    </method>
    <method name='Event'>
      <arg type='i' direction='in'/>
      <arg type='s' direction='in'/>
      <arg type='v' direction='in'/>
      <arg type='u' direction='in'/>
    </method>
  </interface>
</node>
"""


def menu_layout(labels: Sequence[str]) -> GLib.Variant:
    """The DBusMenu tree: one flat item per label, numbered from 1 (0 is root)."""
    children = [
        GLib.Variant("(ia{sv}av)", (item_id, {"label": GLib.Variant("s", label)}, []))
        for item_id, label in enumerate(labels, start=1)
    ]
    root = {"children-display": GLib.Variant("s", "submenu")}
    return GLib.Variant("(ia{sv}av)", (0, root, children))


class TrayIcon:
    """A tray item for `app`; each menu entry activates one of its actions.

    `items` are (label, action name) pairs -- clicking one activates
    `app.<action>`. Clicking the icon itself activates the application, which
    raises the window the background mode hid.
    """

    def __init__(self, app: Gio.Application, items: Sequence[tuple[str, str]]) -> None:
        self._app = app
        self._items = items
        app_id = app.get_application_id() or ""
        name = GLib.get_application_name() or app_id
        self._properties = {
            "Category": GLib.Variant("s", "Communications"),
            "Id": GLib.Variant("s", app_id),
            "Title": GLib.Variant("s", name),
            "Status": GLib.Variant("s", "Active"),
            "IconName": GLib.Variant("s", app_id),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", (app_id, [], name, "")),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        # node.interfaces is _XML's order: the item first, then its menu.
        node = Gio.DBusNodeInfo.new_for_xml(_XML)
        paths = (ITEM_PATH, MENU_PATH)
        for path, interface in zip(paths, node.interfaces, strict=True):
            bus.register_object(path, interface, self._on_call, self._get, None)
        # Registering on "appeared" covers both a watcher that is already there
        # and a shell restarted under us, which drops its item list.
        Gio.bus_watch_name(
            Gio.BusType.SESSION,
            WATCHER_NAME,
            Gio.BusNameWatcherFlags.NONE,
            self._on_watcher_appeared,
            None,
        )

    def _on_watcher_appeared(
        self, bus: Gio.DBusConnection, _name: str, owner: str
    ) -> None:
        logger.debug("registering the tray item with the watcher at %s", owner)
        bus.call(
            WATCHER_NAME,
            "/StatusNotifierWatcher",
            WATCHER_NAME,
            "RegisterStatusNotifierItem",
            GLib.Variant("(s)", (ITEM_PATH,)),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            None,
        )

    def _get(
        self,
        _bus: Gio.DBusConnection,
        _sender: str,
        _path: str,
        interface: str,
        name: str,
    ) -> GLib.Variant | None:
        if interface == MENU_INTERFACE:
            return (
                GLib.Variant("u", 3)
                if name == "Version"
                else GLib.Variant("s", "normal")
            )
        return self._properties.get(name)

    def _on_call(
        self,
        _bus: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _interface: str,
        method: str,
        params: GLib.Variant,
        invocation: Gio.DBusMethodInvocation,
    ) -> None:
        match method:
            case "Activate":
                self._app.activate()
                invocation.return_value(None)
            case "GetLayout":
                layout = menu_layout([label for label, _action in self._items])
                # new_tuple, not a format string: a built Variant cannot be
                # nested inside one.
                invocation.return_value(
                    GLib.Variant.new_tuple(GLib.Variant("u", 1), layout)
                )
            case "GetGroupProperties":
                ids = params.unpack()[0]
                props = [
                    (item_id, {"label": GLib.Variant("s", label)})
                    for item_id, (label, _action) in enumerate(self._items, start=1)
                    if not ids or item_id in ids
                ]
                invocation.return_value(GLib.Variant("(a(ia{sv}))", (props,)))
            case "AboutToShow":
                invocation.return_value(GLib.Variant("(b)", (False,)))
            case "Event":
                item_id, event, _data, _timestamp = params.unpack()
                if event == "clicked" and 1 <= item_id <= len(self._items):
                    self._app.activate_action(self._items[item_id - 1][1], None)
                invocation.return_value(None)
            case _:
                invocation.return_dbus_error(
                    "org.freedesktop.DBus.Error.UnknownMethod", method
                )
