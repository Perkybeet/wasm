"""Tests for web UI components and structure."""

import pytest
import os
from pathlib import Path


# Get the project root
PROJECT_ROOT = Path(__file__).parent.parent
STATIC_DIR = PROJECT_ROOT / "src" / "wasm" / "web" / "static"
JS_DIR = STATIC_DIR / "js"
CSS_DIR = STATIC_DIR / "css"


class TestWebStaticFiles:
    """Test that all required static files exist."""

    def test_static_directory_exists(self):
        """Test that static directory exists."""
        assert STATIC_DIR.exists(), "Static directory should exist"

    def test_index_html_exists(self):
        """Test that index.html exists."""
        index_path = STATIC_DIR / "index.html"
        assert index_path.exists(), "index.html should exist"

    def test_login_html_exists(self):
        """Test that login.html exists."""
        login_path = STATIC_DIR / "login.html"
        assert login_path.exists(), "login.html should exist"

    def test_main_css_exists(self):
        """Test that main.css exists."""
        css_path = CSS_DIR / "main.css"
        assert css_path.exists(), "main.css should exist"

    def test_main_js_exists(self):
        """Test that main.js exists."""
        js_path = JS_DIR / "main.js"
        assert js_path.exists(), "main.js should exist"


class TestJavaScriptCoreModules:
    """Test that all core JavaScript modules exist."""

    @pytest.fixture
    def core_dir(self):
        return JS_DIR / "core"

    def test_api_module_exists(self, core_dir):
        """Test that api.js exists."""
        assert (core_dir / "api.js").exists()

    def test_router_module_exists(self, core_dir):
        """Test that router.js exists."""
        assert (core_dir / "router.js").exists()

    def test_ui_module_exists(self, core_dir):
        """Test that ui.js exists."""
        assert (core_dir / "ui.js").exists()

    def test_websocket_module_exists(self, core_dir):
        """Test that websocket.js exists."""
        assert (core_dir / "websocket.js").exists()

    def test_search_module_exists(self, core_dir):
        """Test that search.js exists (global search)."""
        assert (core_dir / "search.js").exists()

    def test_shortcuts_module_exists(self, core_dir):
        """Test that shortcuts.js exists (keyboard shortcuts)."""
        assert (core_dir / "shortcuts.js").exists()

    def test_theme_module_exists(self, core_dir):
        """Test that theme.js exists (theme toggle)."""
        assert (core_dir / "theme.js").exists()

    def test_notifications_module_exists(self, core_dir):
        """Test that notifications.js exists (notification center)."""
        assert (core_dir / "notifications.js").exists()

    def test_dialogs_module_exists(self, core_dir):
        """Test that dialogs.js exists (styled confirmation dialogs)."""
        assert (core_dir / "dialogs.js").exists()


class TestJavaScriptPageModules:
    """Test that all page JavaScript modules exist."""

    @pytest.fixture
    def pages_dir(self):
        return JS_DIR / "pages"

    def test_dashboard_page_exists(self, pages_dir):
        """Test that dashboard.js exists."""
        assert (pages_dir / "dashboard.js").exists()

    def test_apps_page_exists(self, pages_dir):
        """Test that apps.js exists."""
        assert (pages_dir / "apps.js").exists()

    def test_services_page_exists(self, pages_dir):
        """Test that services.js exists."""
        assert (pages_dir / "services.js").exists()

    def test_sites_page_exists(self, pages_dir):
        """Test that sites.js exists."""
        assert (pages_dir / "sites.js").exists()

    def test_certs_page_exists(self, pages_dir):
        """Test that certs.js exists."""
        assert (pages_dir / "certs.js").exists()

    def test_monitor_page_exists(self, pages_dir):
        """Test that monitor.js exists."""
        assert (pages_dir / "monitor.js").exists()

    def test_logs_page_exists(self, pages_dir):
        """Test that logs.js exists."""
        assert (pages_dir / "logs.js").exists()

    def test_jobs_page_exists(self, pages_dir):
        """Test that jobs.js exists."""
        assert (pages_dir / "jobs.js").exists()

    def test_backups_page_exists(self, pages_dir):
        """Test that backups.js exists."""
        assert (pages_dir / "backups.js").exists()

    def test_config_page_exists(self, pages_dir):
        """Test that config.js exists."""
        assert (pages_dir / "config.js").exists()


class TestJavaScriptComponents:
    """Test that all component modules exist."""

    @pytest.fixture
    def components_dir(self):
        return JS_DIR / "components"

    def test_metrics_component_exists(self, components_dir):
        """Test that metrics.js exists."""
        assert (components_dir / "metrics.js").exists()

    def test_cards_component_exists(self, components_dir):
        """Test that cards.js exists."""
        assert (components_dir / "cards.js").exists()

    def test_jobs_component_exists(self, components_dir):
        """Test that jobs.js exists."""
        assert (components_dir / "jobs.js").exists()

    def test_skeleton_component_exists(self, components_dir):
        """Test that skeleton.js exists (skeleton loading)."""
        assert (components_dir / "skeleton.js").exists()


class TestIndexHtmlContent:
    """Test that index.html has required elements."""

    @pytest.fixture
    def index_content(self):
        index_path = STATIC_DIR / "index.html"
        return index_path.read_text()

    def test_has_sidebar(self, index_content):
        """Test that index.html has sidebar."""
        assert 'class="sidebar' in index_content or 'id="sidebar"' in index_content

    def test_has_navigation(self, index_content):
        """Test that index.html has navigation items."""
        assert 'nav-item' in index_content

    def test_has_dashboard_page(self, index_content):
        """Test that index.html has dashboard page."""
        assert 'page-dashboard' in index_content

    def test_has_toast_container(self, index_content):
        """Test that index.html has toast container."""
        assert 'toast-container' in index_content

    def test_has_modals(self, index_content):
        """Test that index.html has modals."""
        assert '-modal' in index_content

    def test_has_quick_actions(self, index_content):
        """Test that index.html has quick actions FAB."""
        assert 'quick-actions' in index_content

    def test_has_mobile_menu(self, index_content):
        """Test that index.html has mobile menu button."""
        assert 'mobile-menu' in index_content or 'toggleMobileSidebar' in index_content

    def test_has_breadcrumbs(self, index_content):
        """Test that index.html has breadcrumbs."""
        assert 'breadcrumb' in index_content

    def test_has_search_functionality(self, index_content):
        """Test that index.html references search functionality."""
        assert 'globalSearch' in index_content or 'search' in index_content.lower()

    def test_has_keyboard_shortcuts_reference(self, index_content):
        """Test that index.html references keyboard shortcuts."""
        assert 'keyboardShortcuts' in index_content or 'Keyboard Shortcuts' in index_content


class TestMainCssContent:
    """Test that main.css has required styles."""

    @pytest.fixture
    def css_content(self):
        css_path = CSS_DIR / "main.css"
        return css_path.read_text()

    def test_has_css_variables(self, css_content):
        """Test that CSS has custom properties."""
        assert '--bg-primary' in css_content
        assert '--text-primary' in css_content

    def test_has_light_theme(self, css_content):
        """Test that CSS has light theme styles."""
        assert 'light-theme' in css_content

    def test_has_skeleton_loading(self, css_content):
        """Test that CSS has skeleton loading styles."""
        assert 'skeleton' in css_content

    def test_has_quick_actions_styles(self, css_content):
        """Test that CSS has quick actions styles."""
        assert 'quick-actions' in css_content

    def test_has_fab_button_styles(self, css_content):
        """Test that CSS has FAB button styles."""
        assert 'fab-button' in css_content

    def test_has_mobile_responsive_styles(self, css_content):
        """Test that CSS has mobile responsive styles."""
        assert '@media' in css_content

    def test_has_breadcrumbs_styles(self, css_content):
        """Test that CSS has breadcrumbs styles."""
        assert 'breadcrumbs' in css_content

    def test_has_animation_keyframes(self, css_content):
        """Test that CSS has animation keyframes."""
        assert '@keyframes' in css_content
        assert 'fadeIn' in css_content or 'slideUp' in css_content


class TestMainJsImports:
    """Test that main.js imports all required modules."""

    @pytest.fixture
    def main_js_content(self):
        main_js_path = JS_DIR / "main.js"
        return main_js_path.read_text()

    def test_imports_api(self, main_js_content):
        """Test that main.js imports api module."""
        assert 'api.js' in main_js_content

    def test_imports_router(self, main_js_content):
        """Test that main.js imports router module."""
        assert 'router.js' in main_js_content

    def test_imports_search(self, main_js_content):
        """Test that main.js imports search module."""
        assert 'search.js' in main_js_content

    def test_imports_shortcuts(self, main_js_content):
        """Test that main.js imports shortcuts module."""
        assert 'shortcuts.js' in main_js_content

    def test_imports_theme(self, main_js_content):
        """Test that main.js imports theme module."""
        assert 'theme.js' in main_js_content

    def test_imports_notifications(self, main_js_content):
        """Test that main.js imports notifications module."""
        assert 'notifications.js' in main_js_content

    def test_imports_dialogs(self, main_js_content):
        """Test that main.js imports dialogs module."""
        assert 'dialogs.js' in main_js_content

    def test_exports_router_globally(self, main_js_content):
        """Test that main.js exports router to window."""
        assert 'window.router' in main_js_content


class TestSearchModuleContent:
    """Test that search.js has required functionality."""

    @pytest.fixture
    def search_content(self):
        search_path = JS_DIR / "core" / "search.js"
        return search_path.read_text()

    def test_has_search_class(self, search_content):
        """Test that search.js has GlobalSearch class."""
        assert 'class GlobalSearch' in search_content

    def test_has_search_modal(self, search_content):
        """Test that search.js creates search modal."""
        assert 'createSearchModal' in search_content

    def test_has_keyboard_shortcut_setup(self, search_content):
        """Test that search.js sets up Ctrl+K shortcut."""
        assert 'Ctrl' in search_content or 'ctrlKey' in search_content

    def test_has_search_functionality(self, search_content):
        """Test that search.js has search method."""
        assert 'search(' in search_content

    def test_has_result_rendering(self, search_content):
        """Test that search.js renders results."""
        assert 'renderResults' in search_content

    def test_exports_global_search(self, search_content):
        """Test that search.js exports globalSearch."""
        assert 'window.globalSearch' in search_content


class TestShortcutsModuleContent:
    """Test that shortcuts.js has required functionality."""

    @pytest.fixture
    def shortcuts_content(self):
        shortcuts_path = JS_DIR / "core" / "shortcuts.js"
        return shortcuts_path.read_text()

    def test_has_shortcuts_class(self, shortcuts_content):
        """Test that shortcuts.js has KeyboardShortcuts class."""
        assert 'class KeyboardShortcuts' in shortcuts_content

    def test_has_shortcut_registration(self, shortcuts_content):
        """Test that shortcuts.js can register shortcuts."""
        assert 'register(' in shortcuts_content

    def test_has_navigation_shortcuts(self, shortcuts_content):
        """Test that shortcuts.js has navigation shortcuts."""
        assert 'g d' in shortcuts_content or 'dashboard' in shortcuts_content.lower()

    def test_has_help_modal(self, shortcuts_content):
        """Test that shortcuts.js has help modal."""
        assert 'showHelp' in shortcuts_content

    def test_exports_globally(self, shortcuts_content):
        """Test that shortcuts.js exports keyboardShortcuts."""
        assert 'window.keyboardShortcuts' in shortcuts_content


class TestThemeModuleContent:
    """Test that theme.js has required functionality."""

    @pytest.fixture
    def theme_content(self):
        theme_path = JS_DIR / "core" / "theme.js"
        return theme_path.read_text()

    def test_has_theme_class(self, theme_content):
        """Test that theme.js has ThemeManager class."""
        assert 'class ThemeManager' in theme_content

    def test_has_toggle_function(self, theme_content):
        """Test that theme.js has toggle function."""
        assert 'toggle(' in theme_content

    def test_has_dark_theme_support(self, theme_content):
        """Test that theme.js supports dark theme."""
        assert 'dark' in theme_content

    def test_has_light_theme_support(self, theme_content):
        """Test that theme.js supports light theme."""
        assert 'light' in theme_content

    def test_uses_local_storage(self, theme_content):
        """Test that theme.js uses localStorage."""
        assert 'localStorage' in theme_content

    def test_exports_globally(self, theme_content):
        """Test that theme.js exports themeManager."""
        assert 'window.themeManager' in theme_content


class TestNotificationsModuleContent:
    """Test that notifications.js has required functionality."""

    @pytest.fixture
    def notifications_content(self):
        notifications_path = JS_DIR / "core" / "notifications.js"
        return notifications_path.read_text()

    def test_has_notification_class(self, notifications_content):
        """Test that notifications.js has NotificationCenter class."""
        assert 'class NotificationCenter' in notifications_content

    def test_has_add_function(self, notifications_content):
        """Test that notifications.js can add notifications."""
        assert 'add(' in notifications_content

    def test_has_mark_read_function(self, notifications_content):
        """Test that notifications.js can mark as read."""
        assert 'markRead' in notifications_content

    def test_has_clear_all_function(self, notifications_content):
        """Test that notifications.js can clear all."""
        assert 'clearAll' in notifications_content

    def test_uses_local_storage(self, notifications_content):
        """Test that notifications.js uses localStorage."""
        assert 'localStorage' in notifications_content

    def test_exports_globally(self, notifications_content):
        """Test that notifications.js exports notificationCenter."""
        assert 'window.notificationCenter' in notifications_content


class TestDialogsModuleContent:
    """Test that dialogs.js has required functionality."""

    @pytest.fixture
    def dialogs_content(self):
        dialogs_path = JS_DIR / "core" / "dialogs.js"
        return dialogs_path.read_text()

    def test_has_confirm_dialog(self, dialogs_content):
        """Test that dialogs.js has showConfirmDialog function."""
        assert 'showConfirmDialog' in dialogs_content

    def test_has_input_dialog(self, dialogs_content):
        """Test that dialogs.js has showInputDialog function."""
        assert 'showInputDialog' in dialogs_content

    def test_returns_promise(self, dialogs_content):
        """Test that dialogs.js uses Promises."""
        assert 'Promise' in dialogs_content

    def test_has_different_types(self, dialogs_content):
        """Test that dialogs.js supports different types."""
        assert 'danger' in dialogs_content
        assert 'warning' in dialogs_content

    def test_exports_globally(self, dialogs_content):
        """Test that dialogs.js exports showConfirmDialog."""
        assert 'window.showConfirmDialog' in dialogs_content
