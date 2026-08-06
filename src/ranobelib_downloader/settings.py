"""
Модуль для управления настройками приложения
"""

import json
import os
import sys
from typing import Any, Dict, Optional

APP_ROOT = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.abspath('.')
USER_DATA_DIR = os.path.join(APP_ROOT, 'user_data')


class Settings:
    """Класс для управления настройками приложения"""

    def __init__(self, settings_file: Optional[str] = None):
        if settings_file is None:
            self._settings_file = os.path.join(USER_DATA_DIR, "settings.json")
        else:
            self._settings_file = settings_file

        self._settings: Dict[str, Any] = {}
        self._defaults: Dict[str, Any] = {
            "download_cover": True,
            "download_images": True,
            "compress_images": True,
            "grayscale_images": False,
            "compression_preset": "desktop",
            "compress_max_size": 1200,
            "image_quality": 90,
            "add_translator": False,
            "group_by_volumes": True,
            "device_profile": "generic",
            "save_directory": "downloads",
            "selected_formats": ["EPUB"],
        }
        self._preset_defaults: Dict[str, int] = {
            "phone": 900,
            "tablet": 1400,
            "x4": 600,
            "kindle": 1000,
            "desktop": 1400,
        }
        self.load()

    @property
    def presets(self) -> Dict[str, int]:
        return dict(self._preset_defaults)

    def _resolve_max_size(self) -> int:
        preset = self._settings.get("compression_preset") or self._defaults.get("compression_preset")
        if preset in self._preset_defaults:
            return self._preset_defaults[preset]
        max_size = self._settings.get("compress_max_size")
        if isinstance(max_size, int) and max_size > 0:
            return max_size
        return 800

    def load(self) -> None:
        """Загрузка настроек из файла"""
        try:
            if os.path.exists(self._settings_file):
                with open(self._settings_file, "r", encoding="utf-8") as f:
                    self._settings = json.load(f)
            else:
                self._settings = self._defaults.copy()
        except Exception as e:
            print(f"⚠️ Ошибка при загрузке настроек: {e}")
            self._settings = self._defaults.copy()

    def save(self) -> None:
        """Сохранение настроек в файл"""
        try:
            with open(self._settings_file, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ Ошибка при сохранении настроек: {e}")

    def get(self, key: str, default: Any = None) -> Any:
        value = self._settings.get(key, default if default is not None else self._defaults.get(key))

        if key == "save_directory":
            if not value:
                value = self._defaults.get(key)

            if not os.path.isabs(value):
                return os.path.abspath(os.path.join(APP_ROOT, value))
            else:
                return os.path.normpath(value)

        if key == "compress_max_size":
            return self._resolve_max_size()

        return value

    def set(self, key: str, value: Any) -> None:
        if key == "save_directory" and value:
            norm_value = os.path.normpath(value)
            try:
                rel_path = os.path.relpath(norm_value, APP_ROOT)
                if not rel_path.startswith("..") and not os.path.splitdrive(rel_path)[0]:
                    value = rel_path
                else:
                    value = norm_value
            except ValueError:
                value = norm_value

        self._settings[key] = value
        self.save()

    def get_all(self) -> Dict[str, Any]:
        """Получение всех настроек в виде словаря с разрешенными путями"""
        settings_copy = self._settings.copy()
        if "save_directory" in settings_copy:
            settings_copy["save_directory"] = self.get("save_directory")
        if "compress_max_size" not in settings_copy:
            settings_copy["compress_max_size"] = self._resolve_max_size()
        return settings_copy

    def set_compression_preset(self, preset: str) -> None:
        if preset not in self._preset_defaults:
            preset = self._defaults.get("compression_preset", "desktop")
        self._settings["compression_preset"] = preset
        self._settings.pop("compress_max_size", None)
        self.save()


settings = Settings()
