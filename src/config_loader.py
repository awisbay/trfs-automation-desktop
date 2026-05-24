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
    node2_name: str = ""
    gsm_node_name: str = ""
    bsc_name: str = ""

    def get_node_names(self) -> List[str]:
        """Return list of active LTE/NR node names (excludes empty strings)."""
        nodes = [self.node_name]
        if self.node2_name:
            nodes.append(self.node2_name)
        return nodes

    def get_all_nodes(self) -> List[str]:
        """Return list of all active node names including GSM (excludes empty strings)."""
        nodes = self.get_node_names()
        if self.gsm_node_name:
            nodes.append(self.gsm_node_name)
        return nodes


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


def _apply_shortcode_paths(config: AppConfig) -> None:
    """Append the site shortcode as a subfolder inside output_dir and screenshots_dir."""
    sc = config.site.shortcode
    config.paths.output_dir = os.path.join(config.paths.output_dir, sc)
    config.paths.screenshots_dir = os.path.join(config.paths.screenshots_dir, sc)
    os.makedirs(os.path.join(config.base_dir, config.paths.output_dir), exist_ok=True)
    os.makedirs(os.path.join(config.base_dir, config.paths.screenshots_dir), exist_ok=True)


def load_config(config_path: str) -> AppConfig:
    """Load configuration from YAML file."""
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    base_dir = os.path.dirname(os.path.abspath(config_path))

    site = SiteConfig(**raw["site"])
    ssh = SSHConfig(**raw["ssh"])

    moshell_data = raw["moshell"]
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
    gsm_node_name: str = "",
    bsc_name: str = "",
    node2_name: str = "",
) -> AppConfig:
    """Build an AppConfig from GUI form values, merging with existing config.yaml defaults."""
    if config_path and os.path.isfile(config_path):
        config = load_config(config_path)
        config.site.shortcode = shortcode
        config.site.node_name = node_name
        config.site.node2_name = node2_name
        config.site.gsm_node_name = gsm_node_name
        config.site.bsc_name = bsc_name
        config.ssh.host = host
        config.ssh.port = port
        config.ssh.username = username
        config.ssh.password = password
        config.paths.commands = os.path.basename(commands_file)
        # Canonicalize moshell login/prompt for the NEW node_name. We cannot
        # rely on .format() here because load_config() already substituted the
        # {node_name} placeholder with the YAML's saved node; the string has
        # no placeholder left. Forcing the canonical forms here guarantees
        # the SSH runner waits for the correct prompt no matter what was in
        # the persisted config or a prior run's session cache.
        config.moshell.login_command = f"amos {node_name}"
        config.moshell.prompt_pattern = f"{node_name}>"
        _apply_shortcode_paths(config)
    else:
        base_dir = os.path.dirname(os.path.abspath(commands_file)) if commands_file else os.getcwd()
        config = AppConfig(
            site=SiteConfig(
                shortcode=shortcode,
                node_name=node_name,
                node2_name=node2_name,
                gsm_node_name=gsm_node_name,
                bsc_name=bsc_name,
            ),
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
        _apply_shortcode_paths(config)

    return config


def get_full_path(config: AppConfig, relative_path: str) -> str:
    """Resolve a relative path against the base directory."""
    return os.path.join(config.base_dir, relative_path)


def create_node_config(config: AppConfig, node_name: str) -> AppConfig:
    """Create a copy of *config* targeting a specific node.

    Replaces the node name in the moshell login command and prompt pattern
    so that an ``MoshellSession`` opened with the returned config connects
    to *node_name* instead of the original node.
    """
    import copy
    node_config = copy.deepcopy(config)
    old_node = config.site.node_name
    node_config.site.node_name = node_name
    # Update moshell login command: replace the old node name with the new one
    node_config.moshell.login_command = node_config.moshell.login_command.replace(
        old_node, node_name
    )
    # Update moshell prompt pattern
    node_config.moshell.prompt_pattern = node_config.moshell.prompt_pattern.replace(
        old_node, node_name
    )
    return node_config
