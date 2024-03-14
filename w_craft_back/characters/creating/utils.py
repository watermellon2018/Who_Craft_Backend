from w_craft_back.movie.project.models import Project

import logging

logger = logging.getLogger(__name__)


def check_exist_project(project_id):

    cur_project = Project.objects.get(id=project_id)
    logger.info(f'Проект найден {project_id}')
    return cur_project