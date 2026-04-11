"""
Load and validate configuration from config.yaml.
"""
import os
import yaml
from dataclasses import dataclass, field
from typing import List


@dataclass
class SiteConfig:
    shortcode: str
    node_name: str


@dataclass
class SSHConfig:
    host: str
    port: int
    username: str
    password: str


@dataclass
class MoshellConfig:
    login_command: str
    prompt_pattern: str
    command_timeout: int


@dataclass
class PathsConfig:
    template: str
    commands: str
    output_dir: str
    screenshots_dir: str


@dataclass
class TerminalStyle:
    bg_color: List[int]
    text_color: List[int]
    header_color: List[int]
    font_size: int
    font: str
    padding: int
    line_spacing: int


@dataclass
class EnmBrowserConfig:
    enabled: bool = False
    url: str = ""
    alarm_url: str = "https://lhgenm1.globetel.com/#alarmoverview/alarmviewer"
    shm_url: str = "https://lhgenm1.globetel.com/#shm/softwareadministration"
    headless: bool = True
    timeout_ms: int = 30000
    viewport_width: int = 1600
    viewport_height: int = 1200
    panel_splitter_left_px: int | None = 266
    band_cell_patterns: dict[str, list[str]] = field(default_factory=dict)
    tabs: List[str] = field(default_factory=lambda: ["NR Cells", "LTE Cells"])


@dataclass
class AppConfig:
    site: SiteConfig
    ssh: SSHConfig
    moshell: MoshellConfig
    paths: PathsConfig
    terminal_style: TerminalStyle
    enm: EnmBrowserConfig | None = None
    base_dir: str = ""


def load_config(config_path: str) -> AppConfig:
    """Load configuration from YAML file."""
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    base_dir = os.path.dirname(os.path.abspath(config_path))

    site = SiteConfig(**raw["site"])
    ssh = SSHConfig(**raw["ssh"])

    moshell_data = raw["moshell"]
    # Substitute node_name in login_command and prompt_pattern
    moshell_data["login_command"] = moshell_data["login_command"].format(
        node_name=site.node_name
    )
    moshell_data["prompt_pattern"] = moshell_data["prompt_pattern"].format(
        node_name=site.node_name
    )
    moshell = MoshellConfig(**moshell_data)

    paths = PathsConfig(**raw["paths"])
    style = TerminalStyle(**raw["terminal_style"])
    enm_raw = raw.get("enm") or None
    if enm_raw:
        if "band_cell_patterns" not in enm_raw and "band_row_suffixes" in enm_raw:
            enm_raw["band_cell_patterns"] = enm_raw["band_row_suffixes"]
        enm = EnmBrowserConfig(**enm_raw)
    else:
        enm = None

    config = AppConfig(
        site=site,
        ssh=ssh,
        moshell=moshell,
        paths=paths,
        terminal_style=style,
        enm=enm,
        base_dir=base_dir,
    )

    # Ensure output and screenshots directories exist
    os.makedirs(os.path.join(base_dir, paths.output_dir), exist_ok=True)
    os.makedirs(os.path.join(base_dir, paths.screenshots_dir), exist_ok=True)

    return config


def build_config_from_form(
    shortcode: str,
    node_name: str,
    host: str,
    port: int = 5023,
    username: str = "",
    password: str = "",
    commands_file: str = "",
    config_path: str | None = None,
) -> AppConfig:
    """Build an AppConfig from GUI form values, merging with existing config.yaml defaults."""
    if config_path and os.path.isfile(config_path):
        config = load_config(config_path)
        config.site.shortcode = shortcode
        config.site.node_name = node_name
        config.ssh.host = host
        config.ssh.port = port
        config.ssh.username = username
        config.ssh.password = password
        config.paths.commands = os.path.basename(commands_file)
        config.moshell.login_command = config.moshell.login_command.replace(
            config.moshell.login_command.split()[1] if config.moshell.login_command.split()[1:] else "",
            node_name,
        ) if "{node_name}" not in config.moshell.login_command else config.moshell.login_command.format(node_name=node_name)
        config.moshell.prompt_pattern = config.moshell.prompt_pattern.format(node_name=node_name)
    else:
        base_dir = os.path.dirname(os.path.abspath(commands_file)) if commands_file else os.getcwd()
        config = AppConfig(
            site=SiteConfig(shortcode=shortcode, node_name=node_name),
            ssh=SSHConfig(
                host=host,
                port=port,
                username=username,
                password=password,
            ),
            moshell=MoshellConfig(
                login_command=f"amos {node_name}",
                prompt_pattern=f"{node_name}>",
                command_timeout=90,
            ),
            paths=PathsConfig(
                template="TEMPLATE_REPORT.xlsx",
                commands=os.path.basename(commands_file) if commands_file else "TRFS commands.txt",
                output_dir="output",
                screenshots_dir="screenshots",
            ),
            terminal_style=TerminalStyle(
                bg_color=[12, 12, 12],
                text_color=[204, 204, 204],
                header_color=[0, 255, 0],
                font_size=13,
                font="Consolas",
                padding=20,
                line_spacing=4,
            ),
            enm=None,
            base_dir=base_dir,
        )
        os.makedirs(os.path.join(base_dir, config.paths.output_dir), exist_ok=True)
        os.makedirs(os.path.join(base_dir, config.paths.screenshots_dir), exist_ok=True)

    return config


def get_full_path(config: AppConfig, relative_path: str) -> str:
    """Resolve a relative path against the base directory."""
    return os.path.join(config.base_dir, relative_path)
