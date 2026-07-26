from __future__ import annotations

from PySide6.QtWidgets import QApplication, QMessageBox

from core.composition import ApplicationContainer
from core.config import Config
from gui.app import MarlenApp


def _app():
    return QApplication.instance() or QApplication([])


def test_main_gui_has_four_user_tabs(monkeypatch, tmp_path):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "gui.db"))
    app = _app()
    config = Config()
    container = ApplicationContainer(config)
    window = MarlenApp(container.adapter, container.queue_worker, config)
    assert [window.menu.item(i).text() for i in range(window.menu.count())] == [
        "Аккаунт",
        "Каналы",
        "Связки",
        "Комментирование",
        "Инструкция",
    ]
    assert window.stack.count() == 5
    window._tray.hide()
    window.deleteLater()
    app.processEvents()
    container.shutdown()


def test_settings_links_and_comment_history_are_persisted(tmp_path):
    from storage.database import Database

    database = Database(tmp_path / "state.db")
    database.set_settings({"telegram.api_id": 123, "telegram.proxy_enabled": "1"})
    assert database.get_setting("telegram.api_id") == "123"
    database.insert_channel({"channel_id": 77, "title": "News"})
    assert database.update_channel_link(77, 88, "News Chat", "Связано")
    channel = database.get_channel_by_id(77)
    assert channel["linked_chat_id"] == 88
    assert channel["linked_chat_title"] == "News Chat"
    task_id = database.insert_task("noop", {})
    database.add_comment_history(task_id, 77, 10, "hello", "Отправлено")
    assert database.get_comment_history(task_id=task_id)[0]["status"] == "Отправлено"


def test_confirm_login_is_blocked_while_queue_owns_telegram_session(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "auth-lock.db"))
    app = _app()
    config = Config()
    container = ApplicationContainer(config)
    window = MarlenApp(container.adapter, container.queue_worker, config)
    view = window.account_view
    view.api_id.setText("123456")
    view.api_hash.setText("hash")
    view.phone.setText("+79990000000")

    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args))
    monkeypatch.setattr(container.adapter, "is_queue_running", lambda: True)
    started = []
    monkeypatch.setattr(
        view, "_start_worker", lambda *args, **kwargs: started.append(True)
    )

    view.confirm_login()

    assert warnings
    assert started == []
    window._tray.hide()
    window.deleteLater()
    app.processEvents()
    container.shutdown()


def test_premium_gui_is_resizable_and_has_activity_panel(monkeypatch, tmp_path):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "responsive.db"))
    app = _app()
    config = Config()
    container = ApplicationContainer(config)
    window = MarlenApp(container.adapter, container.queue_worker, config)
    window.resize(760, 620)
    window.show()
    app.processEvents()

    assert window.minimumWidth() <= 760
    assert window.minimumHeight() <= 620
    assert window.activity_panel.isVisible()
    assert window.activity_panel.feed.isReadOnly()
    # The panel starts with a placeholder and replaces it as soon as the first
    # background snapshot arrives. Both are valid initial states, and which one
    # is on screen after processEvents() depends on thread-pool timing, so the
    # assertion covers the journal being populated with either of them.
    journal = window.activity_panel.feed.toPlainText()
    assert "Журнал готов" in journal or "Telegram-аккаунт не подключён" in journal
    assert window.vertical_splitter.count() == 2
    assert window.horizontal_splitter.count() == 2

    window._tray.hide()
    # Not close(): closing now asks the operator to confirm, and a modal
    # question in a headless run waits forever. This is teardown, not a test of
    # the close path - that lives in test_v490_closing_really_closes.py.
    window.hide()
    window.deleteLater()
    app.processEvents()
    container.shutdown()


def test_network_wait_campaign_can_be_stopped_from_gui(monkeypatch, tmp_path):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "network-stop.db"))
    app = _app()
    config = Config()
    container = ApplicationContainer(config)
    window = MarlenApp(container.adapter, container.queue_worker, config)
    view = window.commenting_view

    monkeypatch.setattr(
        container.adapter,
        "get_comment_campaign_state",
        lambda: {"id": 1, "status": "network_wait"},
    )
    stopped = []
    monkeypatch.setattr(
        container.adapter,
        "stop_comment_campaign",
        lambda: stopped.append(True) or True,
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(view, "refresh_campaign", lambda: None)

    view.stop_campaign()

    assert stopped == [True]
    window._tray.hide()
    window.deleteLater()
    app.processEvents()
    container.shutdown()


def test_save_comments_button_persists_without_starting_campaign(monkeypatch, tmp_path):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "comments-save.db"))
    app = _app()
    config = Config()
    container = ApplicationContainer(config)
    window = MarlenApp(container.adapter, container.queue_worker, config)
    view = window.commenting_view

    view.editors[0].setPlainText("  Первый комментарий  ")
    view.editors[4].setPlainText("Пятый комментарий\n")
    assert view._comments_dirty is True
    assert "несохранённые" in view.save_status.text().lower()

    view.save_comments_button.click()
    app.processEvents()

    assert container.adapter.get_main_comments() == [
        "Первый комментарий",
        "",
        "",
        "",
        "Пятый комментарий",
        "",
        "",
        "",
        "",
        "",
    ]
    assert container.adapter.get_comment_campaign_state() is None
    assert view._comments_dirty is False
    assert "сохранены" in view.save_status.text().lower()

    window._tray.hide()
    window.deleteLater()
    app.processEvents()
    container.shutdown()


def test_unsaved_comment_text_is_not_overwritten_by_periodic_reload(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "comments-dirty.db"))
    app = _app()
    config = Config()
    container = ApplicationContainer(config)
    container.adapter.save_comment_template(["Сохранённый текст", "", "", "", ""])
    window = MarlenApp(container.adapter, container.queue_worker, config)
    view = window.commenting_view

    view.editors[0].setPlainText("Новый несохранённый текст")
    assert view._comments_dirty is True
    view.load_comments()

    assert view.editors[0].toPlainText() == "Новый несохранённый текст"

    window._tray.hide()
    window.deleteLater()
    app.processEvents()
    container.shutdown()


def test_account_switch_is_blocked_while_persistent_campaign_is_active(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "account-campaign-lock.db"))
    app = _app()
    config = Config()
    container = ApplicationContainer(config)
    window = MarlenApp(container.adapter, container.queue_worker, config)
    view = window.account_view

    monkeypatch.setattr(container.adapter, "is_queue_running", lambda: False)
    monkeypatch.setattr(
        container.adapter,
        "get_comment_campaign_state",
        lambda: {"id": 1, "status": "paused"},
    )
    monkeypatch.setattr(container.adapter, "get_join_campaign_state", lambda: None)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args))
    started = []
    monkeypatch.setattr(
        view, "_start_worker", lambda *args, **kwargs: started.append(True)
    )

    view.request_code()

    assert warnings
    assert "остановите активную кампанию" in str(warnings[0][2]).lower()
    assert started == []
    window._tray.hide()
    window.deleteLater()
    app.processEvents()
    container.shutdown()


def test_comment_daily_limit_slider_persists_locally(monkeypatch, tmp_path):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "limit-slider.db"))
    app = _app()
    config = Config()
    container = ApplicationContainer(config)
    window = MarlenApp(container.adapter, container.queue_worker, config)
    view = window.commenting_view

    assert view.daily_limit_slider.minimum() == 0
    assert view.daily_limit_slider.maximum() == 1000
    view.daily_limit_slider.setValue(137)
    assert view._save_daily_limit() is True
    assert container.adapter.get_comment_daily_limit() == 137

    window._tray.hide()
    window.deleteLater()
    app.processEvents()
    container.shutdown()

    from storage.database import Database

    reopened = Database(tmp_path / "limit-slider.db")
    assert reopened.get_setting("commenting.daily_limit") == "137"


def test_cached_account_transient_check_does_not_show_false_error(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "auth-transient.db"))
    app = _app()
    config = Config()
    container = ApplicationContainer(config)
    window = MarlenApp(container.adapter, container.queue_worker, config)
    view = window.account_view
    cached = {
        "telegram.account_id": "123",
        "telegram.account_name": "Saved Account",
        "telegram.account_username": "saved_user",
        "telegram.authorized": "1",
    }
    view._cached_account_values = dict(cached)
    view._set_authorized_ui(cached)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args))

    view._temporary_failed("Telegram временно не ответил при проверке подключения")

    assert warnings == []
    assert view.status_label.text() == "Аккаунт подключён"
    assert "сессия сохранена" in view.account_label.text()
    assert view.connect_button.text() == "Проверить подключение"

    window._tray.hide()
    window.deleteLater()
    app.processEvents()
    container.shutdown()
