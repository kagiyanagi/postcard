from postcard.core.tray import menu_layout


def test_menu_layout_numbers_items_from_one() -> None:
    """Item 0 is the root, so the entries have to start at 1 -- Event maps the
    id a host sends back to an action by that offset."""
    revision, layout = 1, menu_layout(["Show Window", "Quit Postcard"])
    root_id, root_props, children = layout.unpack()

    assert revision == 1
    assert root_id == 0
    assert root_props == {"children-display": "submenu"}
    assert children == [
        (1, {"label": "Show Window"}, []),
        (2, {"label": "Quit Postcard"}, []),
    ]


def test_menu_layout_matches_the_dbusmenu_signature() -> None:
    assert menu_layout([]).get_type_string() == "(ia{sv}av)"
