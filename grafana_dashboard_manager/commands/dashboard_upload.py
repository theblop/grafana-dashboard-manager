"""
Copyright (c) 2024 BEAM CONNECTIVITY LIMITED

Use of this source code is governed by an MIT-style
license that can be found in the LICENSE file or at
https://opensource.org/licenses/MIT.
"""
import json
import logging
from pathlib import Path

from grafana_dashboard_manager.global_config import GlobalConfig
from grafana_dashboard_manager.grafana.grafana_api import GrafanaApi
from grafana_dashboard_manager.models.folder import Folder

from ..utils import confirm, show_dashboards

logger = logging.getLogger(__name__)


def upload_dashboards(config: GlobalConfig, client: GrafanaApi):
    """CLI command handler to take a source directory of dashboards and write them to Grafana via the HTTP API"""
    source_dir = config.source

    if not isinstance(source_dir, Path):
        raise ValueError(f"Unsupported source: {source_dir=}")

    logger.info(f"Uploading dashboards from {source_dir}")
    show_dashboards(source_dir)

    # Load the folders.json information if present, but otherwise we can still query for the folders with the caveat
    # being folderUids won't be consistent across Grafana installs
    folder_info_dir = source_dir / "folders.json"
    if not (folder_info_dir.exists() and folder_info_dir.is_file()):
        logger.warning(f"The {folder_info_dir} file is missing, which is created when downloading dashboards")
        logger.warning("The folders will not have the same folderUid and links/bookmarks will break")
        folder_info: dict[str, Folder] = {x.title: Folder.model_validate(x) for x in client.folders.all_folders()}
    else:
        with folder_info_dir.open("r") as file:
            folder_info = {key: Folder.model_validate(value) for key, value in json.loads(file.read()).items()}

    if config.non_interactive is False:
        confirm("Folder hierarchy will be preserved. Press any key to confirm upload...")

    for folder in source_dir.glob("*"):
        if not folder.is_dir():
            continue

        # Create the folders using a known folderUid, either from the local file or from a live install
        known_folder = folder_info.get(folder.name)

        if known_folder:
            client.folders.create(folder.name, known_folder.uid)
        # For cases where the folder isn't present in either, then we can just create it and use the autogenerated
        # folderUid. In this scenario, the source dashboards may contain references to folders which now have a
        # different folderUid.
        else:
            _folder = client.folders.create(folder.name)
            folder_info[_folder.title] = _folder

        # Create the folders and dashboards
        for json_file in folder.iterdir():
            logger.info(f"{folder.name}: adding dashboard {json_file.name}")
            with json_file.open("r") as file:
                dashboard = json.loads(file.read())

            dashboard = update_dashlist_folder_ids(dashboard, folder_info)
            client.dashboards.create(dashboard=dashboard, folder_uid=folder_info[folder.name].uid)

    set_home_dashboard(config, client, folder_info)


def set_home_dashboard(config: GlobalConfig, client: GrafanaApi, folder_info: dict[str, Folder]):
    """Uploads the home.json dashboard"""
    if config.source is None:
        logger.warning("No source directory, cannot find home.json file")
        return

    home_dashboard = config.source / "home.json"

    if not home_dashboard.is_file():
        logger.warning(f"{home_dashboard} is not a file")
        return

    with home_dashboard.open("r") as file:
        dashboard = json.loads(file.read())
        dashboard = update_dashlist_folder_ids(dashboard, folder_info)

        dashboard_uid = client.dashboards.create_home(dashboard)
        logger.info(f"Set home dashboard: {dashboard['title']}")

    client.dashboards.set_home(dashboard_uid)


def update_dashlist_folder_ids(dashboard: dict, folder_info: dict[str, Folder]) -> dict:
    """
    Checks consistency between the id of folders in the database with the dashlist panel definitions,
    updating if necessary.
    """
    # Some dashboards use a list of "rows" with "panels" nested within, some dashboards just use "panels".
    # If using "rows", we need to iterate over "rows" to extract all of the "panels"
    if dashboard.get("panels"):
        dashboard["panels"] = [update_panel_dashlist_folder_ids(panel, folder_info) for panel in dashboard["panels"]]

    elif dashboard.get("rows"):
        for row in dashboard["rows"]:
            dashboard["rows"]["panels"] = [
                update_panel_dashlist_folder_ids(panel, folder_info) for panel in row["panels"]
            ]
    else:
        logger.info(f"{dashboard['title']} does not have any any panels")

    return dashboard


def update_panel_dashlist_folder_ids(panel: dict, folder_info: dict[str, Folder]) -> dict:
    """Updates folder ID/UID for dashlist panels"""
    if panel["type"] != "dashlist":
        return panel

    folder_name = panel["title"]
    folder = folder_info.get(folder_name)

    # If there's no folder, it could be referencing other things like recent dashboards, alerts etc
    if folder is None:
        logger.debug(f"Panel {folder_name} was not found in folders")
        return panel

    # Look up the target folder using the panel title - it needs to match!
    logger.info(f"Updating Panel {folder_name} with {folder.uid=} and {folder.id=}")

    # Ensure that the folder id and uid in the dashboard definition matches
    if panel_folder_id := panel["options"].get("folderId"):
        if panel_folder_id != folder.id:
            logger.info(f"Updating folderId to {folder.id}")
            panel["options"]["folderId"] = folder.id

    if panel_folder_uid := panel["options"].get("folderUID"):
        if panel_folder_uid != folder.uid:
            logger.info(f"Updating folderUID to {folder.uid}")
            panel["options"]["folderUID"] = folder.uid

    return panel