# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.conf import settings
from catmaid.control.dvid import get_server_info

from six.moves import map, zip

class DVIDDimension:
    def __init__(self, x, y, z):
        self.x = x
        self.y = y
        self.z = z

class DVIDProject:
    def __init__(self, id):
        self.id = id
        self.title = id

class DVIDStack:
    def __init__(self, project_id, stack_id, stack_data, source_data):
        self.project = project_id
        self.id = stack_id
        self.title = stack_id
        dvid_url = settings.DVID_URL.rstrip('/')
        levels = stack_data['Extended']['Levels']
        r = levels['0']['Resolution']
        self.downsample_factors = [
            [a / b for (a, b) in zip(levels[str(k)]['Resolution'], r)]
            for k in sorted(map(int, levels.keys()))] # Convert to int to prevent lexographic sort.
        self.num_zoom_levels = len(levels.keys()) - 1
        self.resolution = DVIDDimension(r[0], r[1], r[2])
        ts = levels['0']['TileSize']
        self.description = ''
        self.metadata = None

        self.mirrors = [{
            'title': 'Default',
            'image_base': 'api/%s/node/%s/%s/tile/' % (dvid_url, project_id, stack_id),
            'file_extension': settings.DVID_FORMAT,
            'tile_source_type': 8, # DVIDImagetileTileSource
            'tile_width': ts[0],
            'tile_height': ts[1],
            'position': 0
        }]

        # Dimensions
        min_point = source_data['Extended']['MinPoint']
        max_point = source_data['Extended']['MaxPoint']
        self.dimension = DVIDDimension(
            int(max_point[0]) - int(min_point[0]),
            int(max_point[1]) - int(min_point[1]),
            int(max_point[2]) - int(min_point[2]))

        # Broken slices
        self.broken_slices = []

class DVIDProjectStacks:
    def __init__(self):
        dvid_url = settings.DVID_URL.rstrip('/')
        self.data = get_server_info(dvid_url)

        # Default to XY orientation
        self.orientation = 0
        # Default to no translation
        self.translation = DVIDDimension(0, 0, 0)

    def get_stack(self, project_id, stack_id):
        stack_data = self.data[project_id]['DataInstances'][stack_id]
        source_id = stack_data['Extended']['Source']
        source_data = self.data[project_id]['DataInstances'][source_id]
        return DVIDStack(project_id, stack_id, stack_data, source_data)

    def get_project(self, project_id):
        return DVIDProject(project_id)
