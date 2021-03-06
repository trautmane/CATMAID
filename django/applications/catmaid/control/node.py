# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import copy
import json
import msgpack
import ujson
import psycopg2.extras

from collections import defaultdict
from abc import ABCMeta

from django.core.serializers.json import DjangoJSONEncoder
from django.conf import settings
from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.shortcuts import get_object_or_404

from rest_framework.decorators import api_view

from catmaid import state
from catmaid.models import (UserRole, Treenode, ClassInstanceClassInstance,
        Review, Project)
from catmaid.control.authentication import requires_user_role, \
        can_edit_all_or_fail
from catmaid.control.common import get_relation_to_id_map, get_request_list

from PIL import Image, ImageDraw
from aggdraw import Draw, Pen, Brush, Font

from six.moves import map as imap
from six import add_metaclass, print_


ORIENTATIONS = {
    'xy': 0,
    'xz': 1,
    'zy': 2
}

class BasicNodeProvider(object):

    def __init__(self, *args, **kwargs):
        self.enabled = kwargs.get('enabled', True)
        self.project_id = kwargs.get('project_id')
        self.orientation = kwargs.get('orientation')
        self.min_width = kwargs.get('min_width')
        self.min_height = kwargs.get('min_height')
        self.min_depth = kwargs.get('min_depth')
        self.max_width = kwargs.get('max_width')
        self.max_height = kwargs.get('max_height')
        self.max_depth = kwargs.get('max_depth')

    def prepare_db_statements(connection=None):
        pass

    def matches(self, params):
        matches = True
        if not self.enabled:
            return False
        if self.project_id:
            matches = matches and params.get('project_id') == self.project_id
        # Orientation means this node provider will only be used if the query
        # bounding box is
        if self.orientation:
            matches = matches and params.get('orientation') == self.orientation
        if self.min_width:
            matches = matches and self.min_width <= params['width']
        if self.min_height:
            matches = matches and self.min_height <= params['height']
        if self.min_depth:
            matches = matches and self.min_depth <= params['depth']
        if self.max_width:
            matches = matches and self.max_width >= params['width']
        if self.max_height:
            matches = matches and self.max_height >= params['height']
        if self.max_depth:
            matches = matches and self.max_depth >= params['depth']

        return matches

    def get_tuples(self, params, project_id, explicit_treenode_ids,
                explicit_connector_ids, include_labels, with_relation_map):
        return _node_list_tuples_query(params, project_id,
                self, explicit_treenode_ids, explicit_connector_ids,
                include_labels, with_relation_map), 'json'


def get_extra_nodes(params, project_id, explicit_treenode_ids,
        explicit_connector_ids, include_labels, with_relation_map):
    explicit_node_params = copy.deepcopy(params)
    # Update params so tha we don't query any nodes spatially
    explicit_node_params['left'] = float('inf')
    explicit_node_params['right'] = float('inf')
    explicit_node_params['top'] = float('inf')
    explicit_node_params['bottom'] = float('inf')
    explicit_node_params['z1'] = float('inf')
    explicit_node_params['z2'] = float('inf')
    explicit_node_params['halfz'] = float('inf')

    node_provider = Postgis2dNodeProvider()
    return node_provider.get_tuples(explicit_node_params, project_id,
        explicit_treenode_ids, explicit_connector_ids, include_labels,
        with_relation_map)

class CachedJsonNodeNodeProvder(BasicNodeProvider):
    """Retrieve cached msgpack data from the node_query_cache table.
    """

    def get_tuples(self, params, project_id, explicit_treenode_ids,
            explicit_connector_ids, include_labels, with_relation_map):
        cursor = connection.cursor()
        # For JSONB type cache, use ujson to decode, this is roughly 2x faster
        psycopg2.extras.register_default_jsonb(loads=ujson.loads)
        cursor.execute("""
            SELECT json_data FROM node_query_cache
            WHERE project_id = %s AND depth = %s
            LIMIT 1
        """, (project_id, params['z1']))
        rows = cursor.fetchone()

        if rows and rows[0]:
            tuples = rows[0]
            # If there are exta nodes required, query them explicitely using a
            # regular Postgis 2D query. Inject the result into cached data.
            if explicit_treenode_ids or explicit_connector_ids:
                extra_tuples, extra_type = get_extra_nodes(params, project_id,
                    explicit_treenode_ids, explicit_connector_ids, include_labels,
                    with_relation_map)
                if extra_type != 'json':
                    raise ValueError("Unexpected type")

                if len(tuples) == 5:
                    tuples.append([extra_tuples])
                elif len(tuples) == 6:
                    tuples[5].append(extra_tuples)
                else:
                    raise ValueError("Unexpected cached JSON tuple format")

            return tuples, 'json'
        else:
            return None, None


class CachedJsonTextNodeProvder(BasicNodeProvider):
    """Retrieve cached msgpack data from the node_query_cache table.
    """

    def get_tuples(self, params, project_id, explicit_treenode_ids,
                explicit_connector_ids, include_labels, with_relation_map):
        cursor = connection.cursor()
        cursor.execute("""
            SELECT json_text_data FROM node_query_cache
            WHERE project_id = %s AND depth = %s
            LIMIT 1
        """, (project_id, params['z1']))
        rows = cursor.fetchone()

        if rows and rows[0]:
            tuples = rows[0]
            # If there are exta nodes required, query them explicitely using a
            # regular Postgis 2D query. Inject the result into cached data.
            if explicit_treenode_ids or explicit_connector_ids:
                extra_tuples, extra_type = get_extra_nodes(params, project_id,
                    explicit_treenode_ids, explicit_connector_ids, include_labels,
                    with_relation_map)
                if extra_type != 'json':
                    raise ValueError("Unexpected type")

                extra_tuples_json = json.dumps(extra_tuples)
                if tuples[-2] == '}':
                    # If cached response doesn't contain any extra nodes, add a
                    # new field
                    tuples = tuples[0:-1] + ', [' + extra_tuples_json + ']]'
                elif tuples[-2] == ']':
                    tuples = tuples[0:-2] + ', ' + extra_tuples_json + ']]'
                else:
                    raise ValueError("Unexpected cached JSON text tuple format")

            return tuples, 'json_text'
        else:
            return None, None


class CachedMsgpackNodeProvder(BasicNodeProvider):
    """Retrieve cached msgpack data from the node_query_cache table.
    """

    def get_tuples(self, params, project_id, explicit_treenode_ids,
                explicit_connector_ids, include_labels, with_relation_map):
        cursor = connection.cursor()
        cursor.execute("""
            SELECT msgpack_data FROM node_query_cache
            WHERE project_id = %s AND depth = %s
            LIMIT 1
        """, (project_id, params['z1']))
        rows = cursor.fetchone()

        if rows and rows[0]:
            tuples = rows[0]
            # If there are exta nodes required, query them explicitely using a
            # regular Postgis 2D query. Inject the result into cached data.
            if explicit_treenode_ids or explicit_connector_ids:
                extra_tuples, extra_type = get_extra_nodes(params, project_id,
                    explicit_treenode_ids, explicit_connector_ids, include_labels,
                    with_relation_map)
                if extra_type != 'json':
                    raise ValueError("Unexpected type")

                # To inject msgpack data, we expect a five element list, which
                # means the first byte is '\x95'. For now an error is raised if,
                # the first byte is something else.
                if tuples[0] != b'\x95':
                    raise ValueError("Unexpected cached Msgpack tuple format")

                extra_msgpack = msgpack.packb([extra_tuples])

                # Extend the five-element list with extra tuples by making it a
                # six element list and just appending the extra list
                tuples = b'\x96' + tuples[1:] + extra_msgpack

            return bytes(tuples), 'msgpack'
        else:
            return None, None


@add_metaclass(ABCMeta)
class PostgisNodeProvider(BasicNodeProvider):
    CONNECTOR_STATEMENT_NAME = 'get_connectors_postgis'
    connector_query = None

    TREENODE_STATEMENT_NAME = 'get_treenodes_postgis'
    treenode_query = None

    # Allows implementation to handle limit settings on its own
    managed_limit = True

    def __init__(self, connection=None, **kwargs):
        """
        If PREPARED_STATEMENTS is false but you want to override that for a few queries at a time,
        include a django.db.connection in the constructor.
        """
        super(PostgisNodeProvider, self).__init__(**kwargs)

        # If a node limit is set, append the LIMIT clause to both queries
        if self.managed_limit and settings.NODE_LIST_MAXIMUM_COUNT:
            treenode_query_template = self.treenode_query + '\nLIMIT {limit}'
            connector_query_template = self.connector_query + '\nLIMIT {limit}'
        else:
            treenode_query_template = self.treenode_query
            connector_query_template = self.connector_query

        # To execute the queries directly through PsycoPg (i.e. not prepared) a
        # different parameter format is used: {left} -> %(left)s.
        treenode_query_params = ['project_id', 'left', 'top', 'z1', 'right',
                                 'bottom', 'z2', 'halfz', 'halfzdiff', 'limit', 'sanitized_treenode_ids']
        self.treenode_query_psycopg = treenode_query_template.format(
            **{k: '%({})s'.format(k) for k in treenode_query_params})

        connector_query_params = ['project_id', 'left', 'top', 'z1', 'right',
                                  'bottom', 'z2', 'halfz', 'halfzdiff', 'limit', 'sanitized_connector_ids']
        self.connector_query_psycopg = connector_query_template.format(
            **{k: '%({})s'.format(k) for k in connector_query_params})

        # Create prepared statement version
        prepare_var_names = {
            'project_id': '$1',
            'left': '$2',
            'top': '$3',
            'z1': '$4',
            'right': '$5',
            'bottom': '$6',
            'z2': '$7',
            'halfz': '$8',
            'halfzdiff': '$9',
            'limit': '$10',
            'sanitized_connector_ids': '$11',
            'sanitized_treenode_ids': '$11'
        }
        self.treenode_query_prepare = treenode_query_template.format(**prepare_var_names)
        self.connector_query_prepare = connector_query_template.format(**prepare_var_names)

        self.prepared_statements = bool(connection) or settings.PREPARED_STATEMENTS

        self.render_server_side = kwargs.get('server_side', False)

        # If PREPARED_STATEMENTS is true, the statements will have been prepared elsewhere
        if connection and not settings.PREPARED_STATEMENTS:
            self.prepare_db_statements(connection)

    def prepare_db_statements(self, connection):
        """Create prepared statements on a given connection. This is mainly useful
        for long lived connections.
        """
        cursor = connection.cursor()
        cursor.execute("""
            -- 1 project id, 2 left, 3 top, 4 z1, 5 right, 6 bottom, 7 z2,
            -- 8 halfz, 9 halfzdiff, 10 limit 11 sanitized_treenode_ids
            PREPARE {} (int, real, real, real,
                    real, real, real, real, real, int, bigint[]) AS
            {}
        """.format(self.TREENODE_STATEMENT_NAME, self.treenode_query_prepare))
        cursor.execute("""
            -- 1 project id, 2 left, 3 top, 4 z1, 5 right, 6 bottom, 7 z2,
            -- 8 halfz, 9 halfzdiff, 10 limit, 11 sanitized connector ids
            PREPARE {} (int, real, real, real,
                    real, real, real, real, real, int, bigint[]) AS
            {}
        """.format(self.CONNECTOR_STATEMENT_NAME, self.connector_query_prepare))

    def get_treenode_data(self, cursor, params, extra_treenode_ids=None):
        """ Selects all treenodes of which links to other treenodes intersect
        with the request bounding box. Will optionally fetch additional
        treenodes.
        """
        params['halfzdiff'] = abs(params['z2'] - params['z1']) * 0.5
        params['halfz'] = params['z1'] + (params['z2'] - params['z1']) * 0.5
        params['sanitized_treenode_ids'] = list(imap(int, extra_treenode_ids or []))

        if self.prepared_statements:
            # Use a prepared statement to get the treenodes
            cursor.execute('''
                EXECUTE {}(%(project_id)s,
                    %(left)s, %(top)s, %(z1)s, %(right)s, %(bottom)s, %(z2)s,
                    %(halfz)s, %(halfzdiff)s, %(limit)s,
                    %(sanitized_treenode_ids)s)
            '''.format(self.TREENODE_STATEMENT_NAME), params)
        else:
            cursor.execute(self.treenode_query_psycopg, params)

        treenodes = cursor.fetchall()
        treenode_ids = [t[0] for t in treenodes]

        return treenode_ids, treenodes

    def get_connector_data(self, cursor, params, missing_connector_ids=None):
        """Selects all connectors that are in or have links that intersect the
        bounding box, or that are in missing_connector_ids.
        """
        params['halfz'] = params['z1'] + (params['z2'] - params['z1']) * 0.5
        params['halfzdiff'] = abs(params['z2'] - params['z1']) * 0.5
        params['sanitized_connector_ids'] = list(imap(int, missing_connector_ids or []))

        if self.prepared_statements:
            # Use a prepared statement to get connectors
            cursor.execute('''
                EXECUTE {}(%(project_id)s,
                    %(left)s, %(top)s, %(z1)s, %(right)s, %(bottom)s, %(z2)s,
                    %(halfz)s, %(halfzdiff)s, %(limit)s,
                    %(sanitized_connector_ids)s)
            '''.format(self.CONNECTOR_STATEMENT_NAME), params)
        else:
            cursor.execute(self.connector_query_psycopg, params)

        return list(cursor.fetchall())


class Postgis3dNodeProvider(PostgisNodeProvider):
    """
    Fetch treenodes with the help of two PostGIS filters: The &&& operator
    to exclude all edges that don't have a bounding box that intersect with
    the query bounding box. This leads to false positives, because edge
    bounding boxes can intersect without the edge actually intersecting. To
    limit the result set, ST_3DDWithin is used. It allows to limit the result
    set by a distance to another geometry. Here it only allows edges that are
    no farther away than half the height of the query bounding box from a
    plane that cuts the query bounding box in half in Z. There are still false
    positives, but much fewer. Even though ST_3DDWithin is used, it seems to
    be enough to have a n-d index available (the query plan says ST_3DDWithin
    wouldn't use a 2-d index in this query, even if present).
    """

    TREENODE_STATEMENT_NAME = PostgisNodeProvider.TREENODE_STATEMENT_NAME + '_3d'
    treenode_query = '''
        WITH bb_edge AS (
            SELECT te.id
             FROM treenode_edge te
             WHERE te.edge &&& ST_MakeLine(ARRAY[
                 ST_MakePoint({left}, {bottom}, {z2}),
                 ST_MakePoint({right}, {top}, {z1})] ::geometry[])
             AND ST_3DDWithin(te.edge, ST_MakePolygon(ST_MakeLine(ARRAY[
                 ST_MakePoint({left},  {top},    {halfz}),
                 ST_MakePoint({right}, {top},    {halfz}),
                 ST_MakePoint({right}, {bottom}, {halfz}),
                 ST_MakePoint({left},  {bottom}, {halfz}),
                 ST_MakePoint({left},  {top},    {halfz})]::geometry[])),
                 {halfzdiff})
             AND te.project_id = {project_id}
        )
        SELECT
            t1.id,
            t1.parent_id,
            t1.location_x,
            t1.location_y,
            t1.location_z,
            t1.confidence,
            t1.radius,
            t1.skeleton_id,
            EXTRACT(EPOCH FROM t1.edition_time),
            t1.user_id
        FROM (
            SELECT id FROM bb_edge
            UNION
            SELECT t.parent_id
            FROM bb_edge e
            JOIN treenode t
               ON t.id = e.id
            UNION
            SELECT UNNEST({sanitized_treenode_ids}::bigint[])
        ) edges(edge_child_id)
        JOIN treenode t1
          ON edge_child_id = t1.id
    '''

    CONNECTOR_STATEMENT_NAME = PostgisNodeProvider.CONNECTOR_STATEMENT_NAME + '_3d'
    connector_query = '''
      SELECT
          c.id,
          c.location_x,
          c.location_y,
          c.location_z,
          c.confidence,
          EXTRACT(EPOCH FROM c.edition_time),
          c.user_id,
          tc.treenode_id,
          tc.relation_id,
          tc.confidence,
          EXTRACT(EPOCH FROM tc.edition_time),
          tc.id
      FROM (SELECT tce.id AS tce_id
            FROM treenode_connector_edge tce
            WHERE tce.edge &&& ST_MakeLine(ARRAY[
                ST_MakePoint({left}, {bottom}, {z2}),
                ST_MakePoint({right}, {top}, {z1})] ::geometry[])
            AND ST_3DDWithin(tce.edge, ST_MakePolygon(ST_MakeLine(ARRAY[
                ST_MakePoint({left},  {top},    {halfz}),
                ST_MakePoint({right}, {top},    {halfz}),
                ST_MakePoint({right}, {bottom}, {halfz}),
                ST_MakePoint({left},  {bottom}, {halfz}),
                ST_MakePoint({left},  {top},    {halfz})]::geometry[])),
                {halfzdiff})
            AND tce.project_id = {project_id}
        ) edges(edge_tc_id)
      JOIN treenode_connector tc
        ON (tc.id = edge_tc_id)
      JOIN connector c
        ON (c.id = tc.connector_id)

      UNION

      SELECT
          c.id,
          c.location_x,
          c.location_y,
          c.location_z,
          c.confidence,
          EXTRACT(EPOCH FROM c.edition_time),
          c.user_id,
          NULL,
          NULL,
          NULL,
          NULL,
          NULL
      FROM (SELECT cg.id AS cg_id
           FROM connector_geom cg
           WHERE cg.geom &&& ST_MakeLine(ARRAY[
               ST_MakePoint({left}, {bottom}, {z2}),
               ST_MakePoint({right}, {top}, {z1})] ::geometry[])
           AND ST_3DDWithin(cg.geom, ST_MakePolygon(ST_MakeLine(ARRAY[
               ST_MakePoint({left},  {top},    {halfz}),
               ST_MakePoint({right}, {top},    {halfz}),
               ST_MakePoint({right}, {bottom}, {halfz}),
               ST_MakePoint({left},  {bottom}, {halfz}),
               ST_MakePoint({left},  {top},    {halfz})]::geometry[])),
               {halfzdiff})
           AND cg.project_id = {project_id}
          UNION SELECT UNNEST({sanitized_connector_ids}::bigint[])
        ) geoms(geom_connector_id)
      JOIN connector c
        ON (geom_connector_id = c.id)
    '''


class Postgis3dBlurryNodeProvider(PostgisNodeProvider):
    """
    Fetch treenodes with the help of two PostGIS filters: The &&& operator to
    exclude all edges that don't have a bounding box that intersect with the
    query bounding box. This leads to false positives, because edge bounding
    boxes can intersect without the edge actually intersecting. To further
    limit the result set to avoid false positives use Postgis3dNodeProvider.
    """

    TREENODE_STATEMENT_NAME = PostgisNodeProvider.TREENODE_STATEMENT_NAME + '_3d'
    treenode_query = '''
        WITH bb_edge AS (
            SELECT te.id
            FROM treenode_edge te
            WHERE te.edge &&& ST_MakeLine(ARRAY[
                ST_MakePoint({left}, {bottom}, {z2}),
                ST_MakePoint({right}, {top}, {z1})] ::geometry[])
            AND te.project_id = {project_id}
        )
        SELECT
            t1.id,
            t1.parent_id,
            t1.location_x,
            t1.location_y,
            t1.location_z,
            t1.confidence,
            t1.radius,
            t1.skeleton_id,
            EXTRACT(EPOCH FROM t1.edition_time),
            t1.user_id
        FROM (
            SELECT id from bb_edge
            UNION
            SELECT t.parent_id
            FROM bb_edge e
            JOIN treenode t
              ON t.id = e.id
            UNION
            SELECT UNNEST({sanitized_treenode_ids}::bigint[])
        ) edges(edge_child_id)
        JOIN treenode t1
          ON edge_child_id = t1.id
    '''

    CONNECTOR_STATEMENT_NAME = PostgisNodeProvider.CONNECTOR_STATEMENT_NAME + '_3d'
    connector_query = '''
      SELECT
          c.id,
          c.location_x,
          c.location_y,
          c.location_z,
          c.confidence,
          EXTRACT(EPOCH FROM c.edition_time),
          c.user_id,
          tc.treenode_id,
          tc.relation_id,
          tc.confidence,
          EXTRACT(EPOCH FROM tc.edition_time),
          tc.id
      FROM (SELECT tce.id AS tce_id
            FROM treenode_connector_edge tce
            WHERE tce.edge &&& ST_MakeLine(ARRAY[
                ST_MakePoint({left}, {bottom}, {z2}),
                ST_MakePoint({right}, {top}, {z1})] ::geometry[])
            AND tce.project_id = {project_id}
        ) edges(edge_tc_id)
      JOIN treenode_connector tc
        ON (tc.id = edge_tc_id)
      JOIN connector c
        ON (c.id = tc.connector_id)

      UNION

      SELECT
          c.id,
          c.location_x,
          c.location_y,
          c.location_z,
          c.confidence,
          EXTRACT(EPOCH FROM c.edition_time),
          c.user_id,
          NULL,
          NULL,
          NULL,
          NULL,
          NULL
      FROM (SELECT cg.id AS cg_id
           FROM connector_geom cg
           WHERE cg.geom &&& ST_MakeLine(ARRAY[
               ST_MakePoint({left}, {bottom}, {z2}),
               ST_MakePoint({right}, {top}, {z1})] ::geometry[])
           AND cg.project_id = {project_id}
          UNION SELECT UNNEST({sanitized_connector_ids}::bigint[])
        ) geoms(geom_connector_id)
      JOIN connector c
        ON (geom_connector_id = c.id)
    '''


class Postgis2dNodeProvider(PostgisNodeProvider):
    """
    Fetch treenodes with the help of two PostGIS filters: First, select all
    edges with a bounding box overlapping the XY-box of the query bounding
    box. This set is then constrained by a particular range in Z. Both filters
    are backed by indices that make these operations very fast. This is
    semantically equivalent with what the &&& does. This, however, leads to
    false positives, because edge bounding boxes can intersect without the
    edge actually intersecting. To limit the result set, ST_3DDWithin is used.
    It allows to limit the result set by a distance to another geometry. Here
    it only allows edges that are no farther away than half the height of the
    query bounding box from a plane that cuts the query bounding box in half
    in Z. There are still false positives, but much fewer. Even though
    ST_3DDWithin is used, it seems to be enough to have a n-d index available
    (the query plan says ST_3DDWithin wouldn't use a 2-d index in this query,
    even if present).
    """

    TREENODE_STATEMENT_NAME = PostgisNodeProvider.TREENODE_STATEMENT_NAME + '_2d'
    treenode_query = """
          WITH z_filtered_edge AS (
            SELECT te.id, te.edge
            FROM treenode_edge te
            WHERE floatrange(ST_ZMin(te.edge),
                 ST_ZMax(te.edge), '[]') && floatrange({z1}, {z2}, '[)')
              AND te.project_id = {project_id}
          ), bb_edge AS (
            SELECT e.id
            FROM z_filtered_edge e
            WHERE e.edge && ST_MakeEnvelope({left}, {top}, {right}, {bottom})
              AND ST_3DDWithin(e.edge, ST_MakePolygon(ST_MakeLine(ARRAY[
                  ST_MakePoint({left},  {top},    {halfz}),
                  ST_MakePoint({right}, {top},    {halfz}),
                  ST_MakePoint({right}, {bottom}, {halfz}),
                  ST_MakePoint({left},  {bottom}, {halfz}),
                  ST_MakePoint({left},  {top},    {halfz})]::geometry[])),
                  {halfzdiff})
          )
          SELECT
            t1.id,
            t1.parent_id,
            t1.location_x,
            t1.location_y,
            t1.location_z,
            t1.confidence,
            t1.radius,
            t1.skeleton_id,
            EXTRACT(EPOCH FROM t1.edition_time),
            t1.user_id
          FROM (
              SELECT id FROM bb_edge
              UNION
              SELECT t.parent_id
              FROM bb_edge e
              JOIN treenode t
                 ON t.id = e.id
              UNION
              SELECT UNNEST({sanitized_treenode_ids}::bigint[])
          ) edges(edge_child_id)
        JOIN treenode t1
          ON edges.edge_child_id = t1.id
    """

    CONNECTOR_STATEMENT_NAME = PostgisNodeProvider.CONNECTOR_STATEMENT_NAME + '_2d'
    connector_query = """
        SELECT
            c.id,
            c.location_x,
            c.location_y,
            c.location_z,
            c.confidence,
            EXTRACT(EPOCH FROM c.edition_time),
            c.user_id,
            tc.treenode_id,
            tc.relation_id,
            tc.confidence,
            EXTRACT(EPOCH FROM tc.edition_time),
            tc.id
        FROM (SELECT tce.id AS tce_id
             FROM treenode_connector_edge tce
             WHERE tce.edge && ST_MakeEnvelope({left}, {top}, {right}, {bottom})
               AND floatrange(ST_ZMin(tce.edge), ST_ZMax(tce.edge), '[]') &&
                 floatrange({z1}, {z2}, '[)')
               AND ST_3DDWithin(tce.edge, ST_MakePolygon(ST_MakeLine(ARRAY[
                   ST_MakePoint({left},  {top},    {halfz}),
                   ST_MakePoint({right}, {top},    {halfz}),
                   ST_MakePoint({right}, {bottom}, {halfz}),
                   ST_MakePoint({left},  {bottom}, {halfz}),
                   ST_MakePoint({left},  {top},    {halfz})]::geometry[])),
                   {halfzdiff})
               AND tce.project_id = {project_id}
          ) edges(edge_tc_id)
        JOIN treenode_connector tc
          ON (tc.id = edge_tc_id)
        JOIN connector c
          ON (c.id = tc.connector_id)

        UNION

        SELECT
            c.id,
            c.location_x,
            c.location_y,
            c.location_z,
            c.confidence,
            EXTRACT(EPOCH FROM c.edition_time),
            c.user_id,
            NULL,
            NULL,
            NULL,
            NULL,
            NULL
        FROM (SELECT cg.id AS cg_id
             FROM connector_geom cg
             WHERE cg.geom && ST_MakeEnvelope({left}, {top}, {right}, {bottom})
               AND floatrange(ST_ZMin(cg.geom), ST_ZMax(cg.geom), '[]') &&
                 floatrange({z1}, {z2}, '[)')
               AND ST_3DDWithin(cg.geom, ST_MakePolygon(ST_MakeLine(ARRAY[
                   ST_MakePoint({left},  {top},    {halfz}),
                   ST_MakePoint({right}, {top},    {halfz}),
                   ST_MakePoint({right}, {bottom}, {halfz}),
                   ST_MakePoint({left},  {bottom}, {halfz}),
                   ST_MakePoint({left},  {top},    {halfz})]::geometry[])),
                   {halfzdiff})
               AND cg.project_id = {project_id}
            UNION SELECT UNNEST({sanitized_connector_ids}::bigint[])
          ) geoms(geom_connector_id)
        JOIN connector c
          ON (geom_connector_id = c.id)
    """


class Postgis2dBlurryNodeProvider(PostgisNodeProvider):
    """
    Fetch treenodes with the help of two PostGIS filters: First, select all
    edges with a bounding box overlapping the XY-box of the query bounding
    box. This set is then constrained by a particular range in Z. Both filters
    are backed by indices that make these operations very fast. This is
    semantically equivalent with what the &&& does. This, however, leads to
    false positives, because edge bounding boxes can intersect without the
    edge actually intersecting. To limit the result set further and reduce the
    number of false positives use Postgis2dNodeProvider.
    """

    TREENODE_STATEMENT_NAME = PostgisNodeProvider.TREENODE_STATEMENT_NAME + '_2d_blurry'
    treenode_query = """
          WITH z_filtered_edge AS (
              SELECT te.id, te.edge
              FROM treenode_edge te
              WHERE floatrange(ST_ZMin(te.edge), ST_ZMax(te.edge), '[]') && floatrange({z1}, {z2}, '[)')
                AND te.project_id = {project_id}
          ), bb_edge AS (
              SELECT te.id
              FROM z_filtered_edge te
              WHERE te.edge && ST_MakeEnvelope({left}, {top}, {right}, {bottom})
          )
          SELECT
            t1.id,
            t1.parent_id,
            t1.location_x,
            t1.location_y,
            t1.location_z,
            t1.confidence,
            t1.radius,
            t1.skeleton_id,
            EXTRACT(EPOCH FROM t1.edition_time),
            t1.user_id
          FROM
            (SELECT id FROM bb_edge
              UNION
              SELECT t.parent_id
              FROM bb_edge e
              JOIN treenode t
                ON t.id = e.id
              UNION
              SELECT UNNEST({sanitized_treenode_ids}::bigint[])
        ) edges(edge_child_id)
        JOIN treenode t1
          ON edges.edge_child_id = t1.id
    """

    CONNECTOR_STATEMENT_NAME = PostgisNodeProvider.CONNECTOR_STATEMENT_NAME + '_2d_blurry'
    connector_query = """
        SELECT
            c.id,
            c.location_x,
            c.location_y,
            c.location_z,
            c.confidence,
            EXTRACT(EPOCH FROM c.edition_time),
            c.user_id,
            tc.treenode_id,
            tc.relation_id,
            tc.confidence,
            EXTRACT(EPOCH FROM tc.edition_time),
            tc.id
        FROM (SELECT tce.id AS tce_id
             FROM treenode_connector_edge tce
             WHERE tce.edge && ST_MakeEnvelope({left}, {top}, {right}, {bottom})
               AND floatrange(ST_ZMin(tce.edge), ST_ZMax(tce.edge), '[]') &&
                 floatrange({z1}, {z2}, '[)')
               AND tce.project_id = {project_id}
          ) edges(edge_tc_id)
        JOIN treenode_connector tc
          ON (tc.id = edge_tc_id)
        JOIN connector c
          ON (c.id = tc.connector_id)

        UNION

        SELECT
            c.id,
            c.location_x,
            c.location_y,
            c.location_z,
            c.confidence,
            EXTRACT(EPOCH FROM c.edition_time),
            c.user_id,
            NULL,
            NULL,
            NULL,
            NULL,
            NULL
        FROM (SELECT cg.id AS cg_id
             FROM connector_geom cg
             WHERE cg.geom && ST_MakeEnvelope({left}, {top}, {right}, {bottom})
               AND floatrange(ST_ZMin(cg.geom), ST_ZMax(cg.geom), '[]') &&
                 floatrange({z1}, {z2}, '[)')
               AND cg.project_id = {project_id}
            UNION SELECT UNNEST({sanitized_connector_ids}::bigint[])
          ) geoms(geom_connector_id)
        JOIN connector c
          ON (geom_connector_id = c.id)
    """

# A map of all available node providers that can be used.
AVAILABLE_NODE_PROVIDERS = {
    'postgis3d': Postgis3dNodeProvider,
    'postgis3dblurry': Postgis3dBlurryNodeProvider,
    'postgis2d': Postgis2dNodeProvider,
    'postgis2dblurry': Postgis2dBlurryNodeProvider,
    'cached_json': CachedJsonNodeNodeProvder,
    'cached_json_text': CachedJsonTextNodeProvder,
    'cached_msgpack': CachedMsgpackNodeProvder,
}


# The database cache identifier for each cache node provider
CACHE_NODE_PROVIDER_DATA_TYPES = {
    'cached_json': 'json',
    'cached_json_text': 'json_text',
    'cached_msgpack': 'msgpack',
}


def get_configured_node_providers(provider_entries, connection=None):
    node_providers = []
    for entry in provider_entries:
        options = {}
        # An entry is allowed to be a two-tuple (name, options) to provide
        # options to the constructor call. Otherwise a simple name string is
        # expected.
        if type(entry) in (list, tuple):
            key = entry[0]
            options = entry[1]
        else:
            key = entry

        Provider = AVAILABLE_NODE_PROVIDERS.get(key)
        if Provider:
            node_providers.append(Provider(connection, **options))
        else:
            raise ValueError('Unknown node provider: ' + key)

    return node_providers


def update_node_query_cache(node_providers=None, log=print_):
    if not node_providers:
        node_providers = settings.NODE_PROVIDERS

    for np in node_providers:
        log("Checking node provder {}".format(np))
        if type(np) in (list, tuple):
            key = np[0]
            options = np[1]
        else:
            key = np
            options = {}

        project_id = options.get('project_id')
        if project_id:
            project_ids = [project_id]
        else:
            project_ids = list(Project.objects.all().order_by('id').values_list('id', flat=True))

        clean_cache = options.get('clean', False)

        data_type = CACHE_NODE_PROVIDER_DATA_TYPES.get(key)
        if not data_type:
            log("Skipping non-caching node provider: {}".format(key))
            continue

        for project_id in project_ids:
            log("Updating cache for project {}".format(project_id))
            orientations = [options.get('orientation', 'xy')]
            steps = [options.get('step')]
            if not steps:
                raise ValueError("Need 'step' parameter in node provider configuration")
            node_limit = options.get('node_limit', None)
            update_cache(project_id, data_type, orientations, steps,
                    node_limit=node_limit, delete=clean_cache, log=log)


def get_tracing_bounding_box(project_id, cursor=None):
    """Return the tracing data bounding box.
    """
    if not cursor:
        cursor = connection.cursor()

    cursor.execute("""
        SELECT ARRAY[ST_XMin(bb.box), ST_YMin(bb.box), ST_ZMin(bb.box)],
               ARRAY[ST_XMax(bb.box), ST_YMax(bb.box), ST_ZMax(bb.box)]
        FROM (
            SELECT ST_3DExtent(edge) box FROM treenode_edge
            WHERE project_id = %(project_id)s
        ) bb;
    """, {
        'project_id': project_id
    })

    row = cursor.fetchone()
    if not row:
        raise ValueError("Could not compute bounding box of project {}".format(project_id))

    return row

def update_cache(project_id, data_type, orientations, steps,
        node_limit=None, delete=False, bb_limits=None, log=print_):
    if data_type not in ('json', 'json_text', 'msgpack'):
        raise ValueError('Type must be one of: json, json_text, msgpack')
    if len(steps) != len(orientations):
        raise ValueError('Need one depth resolution flag per orientation')
    if project_id is None:
        raise ValueError('Need project ID')

    orientation_ids = list(map(lambda x: ORIENTATIONS[x], orientations))

    cursor = connection.cursor()

    log(' -> Finding tracing data bounding box')
    row = get_tracing_bounding_box(project_id, cursor)
    bb = [row[0], row[1]]
    if None in bb[0] or None in bb[1]:
        log(' -> Found no valid bounding box, skipping project: {}'.format(bb))
        return
    else:
        log(' -> Found bounding box: {}'.format(bb))

    if bb_limits:
        bb[0][0] = max(bb[0][0], bb_limits[0][0])
        bb[0][1] = max(bb[0][1], bb_limits[0][1])
        bb[0][2] = max(bb[0][2], bb_limits[0][2])
        bb[1][0] = min(bb[1][0], bb_limits[1][0])
        bb[1][1] = min(bb[1][1], bb_limits[1][1])
        bb[1][2] = min(bb[1][2], bb_limits[1][2])
        log(' -> Applied limits to bounding box: {}'.format(bb))

    if delete:
        for n, o in enumerate(orientations):
            orientation_id = orientation_ids[n]
            log(' -> Deleting existing cache entries in orientation {}'.format(project_id, o))
            cursor.execute("""
                DELETE FROM node_query_cache
                WHERE project_id = %(project_id)s
                AND orientation = %(orientation)s
            """, {
                'project_id': project_id,
                'orientation': orientation_id
            })

    params = {
        'left': bb[0][0],
        'top': bb[0][1],
        'z1': None,
        'right': bb[1][0],
        'bottom': bb[1][1],
        'z2': None,
        'project_id': project_id,
        'limit': node_limit
    }

    min_z = bb[0][2]
    max_z = bb[1][2]

    data_types = [data_type]
    update_json_cache = 'json' in data_types
    update_json_text_cache = 'json_text' in data_types
    update_msgpack_cache = 'msgpack' in data_types

    provider = Postgis2dNodeProvider()
    types = ', '.join(data_types)

    for o, step in zip(orientations, steps):
        orientation_id = ORIENTATIONS[o]
        log(' -> Populating cache for orientation {} with depth resolution {} for types: {}'.format(o, step, types))
        z = min_z
        while z < max_z:
            params['z1'] = z
            params['z2'] = z + step
            result_tuple = _node_list_tuples_query(params, project_id, provider)

            if update_json_cache:
                data = ujson.dumps(result_tuple)
                cursor.execute("""
                    INSERT INTO node_query_cache (project_id, orientation, depth, update_time, json_data)
                    VALUES (%s, %s, %s, now(), %s)
                    ON CONFLICT (project_id, orientation, depth)
                    DO UPDATE SET json_data = EXCLUDED.json_data, update_time = EXCLUDED.update_time;
                """, (project_id, orientation_id, z, json.dumps(result_tuple)))

            if update_json_text_cache:
                data = ujson.dumps(result_tuple)
                cursor.execute("""
                    INSERT INTO node_query_cache (project_id, orientation, depth, update_time, json_text_data)
                    VALUES (%s, %s, %s, now(), %s)
                    ON CONFLICT (project_id, orientation, depth)
                    DO UPDATE SET json_text_data = EXCLUDED.json_text_data, update_time = EXCLUDED.update_time;
                """, (project_id, orientation_id, z, json.dumps(result_tuple)))

            if update_msgpack_cache:
                data = msgpack.packb(result_tuple)
                cursor.execute("""
                    INSERT INTO node_query_cache (project_id, orientation, depth, update_time, msgpack_data)
                    VALUES (%s, %s, %s, now(), %s)
                    ON CONFLICT (project_id, orientation, depth)
                    DO UPDATE SET msgpack_data = EXCLUDED.msgpack_data, update_time = EXCLUDED.update_time;
                """, (project_id, orientation_id, z, psycopg2.Binary(data)))

            z += step


def prepare_db_statements(connection):
    node_providers = get_configured_node_providers(settings.NODE_PROVIDERS, connection)
    for node_provider in node_providers:
        node_provider.prepare_db_statements(connection)


@requires_user_role([UserRole.Annotate, UserRole.Browse])
def node_list_tuples(request, project_id=None, provider=None):
    '''Retrieve all nodes intersecting a bounding box

    The intersection bounding box is defined in terms of its minimum and
    maximum project space coordinates. The number of returned nodes can be
    limited to constrain query execution time. Optionally, lists of treenodes
    and connector IDs can be provided to make sure they are included in the
    result set, regardless of intersection.

    Returned is an array with five entries, plus optionally a sixth one

    [[treenodes], [connectors], {labels}, node_limit_reached, {relation_map}, {exstraNodes}]

    The list of treenodes has elements of this form:

    [id, parent_id, location_x, location_y, location_z, confidence, radius, skeleton_id, edition_time, user_id]

    The list connectors has elements of this form:

    [id, location_x, location_y, location_z, confidence, edition_time, user_id, [partners]]

    The partners arrary represents linked partner nodes, each one represented like this:

    [treenode_id, relation_id, link_confidence, link_edition_time, link_id]

    If labels are returned, they are represented as an object of the following
    form, with the labels just being simple strings:

    {treenode_id: [labels]}

    The fourth top level entry, node_limit_reached, is a boolean that
    represents if there are more nodes available than the ones returned.

    With the last top level element returned the present connector linked
    relations are mapped to their textural representations:

    {relation_id: relation_name}
    ---
    parameters:
    - name: treenode_ids
      description: |
        Whether linked connectors should be returned.
      required: false
      type: array
      items:
        type: integer
      paramType: form
    - name: connector_ids
      description: |
        Whether tags should be returned.
      required: false
      type: array
      items:
        type: integer
      paramType: form
    - name: limit
      description: |
        Limit the number of returned nodes.
      required: false
      type: integer
      defaultValue: 3500
      paramType: form
    - name: left
      description: |
        Minimum world space X coordinate
      required: true
      type: float
      paramType: form
    - name: top
      description: |
        Minimum world space Y coordinate
      required: true
      type: float
      paramType: form
    - name: z1
      description: |
        Minimum world space Z coordinate
      required: true
      type: float
      paramType: form
    - name: right
      description: |
        Maximum world space X coordinate
      required: true
      type: float
      paramType: form
    - name: bottom
      description: |
        Maximum world space Y coordinate
      required: true
      type: float
      paramType: form
    - name: z2
      description: |
        Maximum world space Z coordinate
      required: true
      type: float
      paramType: form
    - name: format
      description: |
        Either "json" (default) or "msgpack", optional.
      required: false
      type: string
      paramType: form
    - name: with_relation_map
      description: |
        Whether an ID to name mapping for the used relations should be included
        and which extent it should have.
      required: false
      enum: [none, used, all]
      type: string
      defaultValue: used
      paramType: form
    type:
    - type: array
      items:
        type: string
      required: true
    '''
    project_id = int(project_id) # sanitize
    if request.method == 'POST':
        data = request.POST
    elif request.method == 'GET':
        data = request.GET
    else:
        raise ValueError("Unsupported HTTP method: " + request.method)

    params = {}

    treenode_ids = get_request_list(data, 'treenode_ids', tuple(), int)
    connector_ids = get_request_list(data, 'connector_ids', tuple(), int)
    for p in ('top', 'left', 'bottom', 'right', 'z1', 'z2'):
        params[p] = float(data.get(p, 0))
    # Limit the number of retrieved treenodes within the section
    params['limit'] = settings.NODE_LIST_MAXIMUM_COUNT
    params['project_id'] = project_id
    include_labels = (data.get('labels', None) == 'true')
    target_format = data.get('format', 'json')
    target_options = {
        'view_width': int(data.get('view_width', 1000)),
        'view_height': int(data.get('view_height', 1000)),
    }
    override_provider = data.get('src')
    with_relation_map = data.get('with_relation_map', 'used')
    if with_relation_map not in ("none", "used", "all"):
        raise ValueError("Relation map can only be 'none', 'used' or 'all'")
    if with_relation_map == 'none':
        with_relation_map = None
    # The orientation parameter is used to override the dominant depth
    # dimension. This dimension is used do some queries.
    orientation = data.get('orientation')
    width = params['width'] = params.get('right') - params.get('left')
    height = params['height'] = params.get('bottom') - params.get('top')
    depth = params['depth'] = params.get('z2') - params.get('z1')
    if not orientation:
        if depth < width:
            if depth < height:
                orientation = 'xy'
            else:
                orientation = 'xz'
        else:
            if depth < height:
                orientation = 'zy'
            elif width < height:
                orientation = 'zy'
            else:
                orientation = 'xz'
    params['orientation'] = orientation

    if override_provider:
        node_providers = get_configured_node_providers([override_provider])
    else:
        node_providers = get_configured_node_providers(settings.NODE_PROVIDERS)

    return compile_node_list_result(project_id, node_providers, params,
        treenode_ids, connector_ids, include_labels, target_format,
        target_options, with_relation_map)


def _node_list_tuples_query(params, project_id, node_provider,
        explicit_treenode_ids=tuple(), explicit_connector_ids=tuple(),
        include_labels=False, with_relation_map=True):
    """The returned JSON data is sensitive to indices in the array, so care
    must be taken never to alter the order of the variables in the SQL
    statements without modifying the accesses to said data both in this
    function and in the client that consumes it.
    """
    try:
        cursor = connection.cursor()

        if with_relation_map or include_labels:
            cursor.execute('''
            SELECT relation_name, id FROM relation WHERE project_id=%s
            ''' % project_id)
            relation_map = dict(cursor.fetchall())
            id_to_relation = {v: k for k, v in relation_map.items()}

        # A set of extra treenode and connector IDs
        missing_treenode_ids = set(n for n in explicit_treenode_ids if n != -1)
        missing_connector_ids = set(c for c in explicit_connector_ids if c != -1)

        # Find connectors in the field of view
        response_on_error = 'Failed to query connector locations.'
        crows = node_provider.get_connector_data(cursor, params,
            missing_connector_ids)

        connectors = []
        # A set of unique connector IDs
        connector_ids = set()

        # Collect links to connectors for each treenode. Each entry maps a
        # relation ID to a an object containing the relation name, and an object
        # mapping connector IDs to confidences.
        links = defaultdict(list)
        used_relations = set()
        seen_links = set()

        for row in crows:
            # Collect treeenode IDs related to connectors but not yet in treenode_ids
            # because they lay beyond adjacent sections
            cid = row[0] # connector ID
            tnid = row[7] # treenode ID
            tcid = row[11] # treenode connector ID

            if tnid is not None:
                if tcid in seen_links:
                    continue
                missing_treenode_ids.add(tnid)
                seen_links.add(tcid)
                # Collect relations between connectors and treenodes
                # row[7]: treenode_id (tnid above)
                # row[8]: treenode_relation_id
                # row[9]: tc_confidence
                # row[10]: tc_edition_time
                # row[11]: tc_id
                links[cid].append(row[7:12])
                used_relations.add(row[8])

            # Collect unique connectors
            if cid not in connector_ids:
                connectors.append(row[0:7] + (links[cid],))
                connector_ids.add(cid)

        response_on_error = 'Failed to query treenodes'

        treenode_ids, treenodes = node_provider.get_treenode_data(cursor,
                params, missing_treenode_ids)
        n_retrieved_nodes = len(treenode_ids)

        labels = defaultdict(list)
        if include_labels:
            # Avoid dict lookups in loop
            top, left, z1 = params['top'], params['left'], params['z1']
            bottom, right, z2 = params['bottom'], params['right'], params['z2']

            def is_visible(r):
                return left <= r[2] < right and \
                    top <= r[3] < bottom and \
                    z1 <= r[4] < z2

            # Collect treenodes visible in the current section
            visible = [row[0] for row in treenodes if is_visible(row)]
            if visible:
                cursor.execute('''
                SELECT treenode_class_instance.treenode_id,
                       class_instance.name
                FROM class_instance,
                     treenode_class_instance,
                     UNNEST(%s::bigint[]) treenodes(tnid)
                WHERE treenode_class_instance.relation_id = %s
                  AND class_instance.id = treenode_class_instance.class_instance_id
                  AND treenode_class_instance.treenode_id = tnid
                ''', (visible, relation_map['labeled_as']))
                for row in cursor.fetchall():
                    labels[row[0]].append(row[1])

            # Collect connectors visible in the current section
            visible = [row[0] for row in connectors if z1 <= row[3] < z2]
            if visible:
                cursor.execute('''
                SELECT connector_class_instance.connector_id,
                       class_instance.name
                FROM class_instance,
                     connector_class_instance,
                     UNNEST(%s::bigint[]) connectors(cnid)
                WHERE connector_class_instance.relation_id = %s
                  AND class_instance.id = connector_class_instance.class_instance_id
                  AND connector_class_instance.connector_id = cnid
                ''', (visible, relation_map['labeled_as']))
                for row in cursor.fetchall():
                    labels[row[0]].append(row[1])

        if with_relation_map == 'used':
            export_relation_map = {r:id_to_relation[r] for r in used_relations}
        elif with_relation_map == 'all':
            export_relation_map = id_to_relation
        else:
            export_relation_map = {}

        result = [treenodes, connectors, labels,
                n_retrieved_nodes == params['limit'], export_relation_map]

        return result

    except Exception as e:
        import traceback
        raise Exception(response_on_error + ':' + str(e) + '\nOriginal error: ' + str(traceback.format_exc()))

def node_list_tuples_query(params, project_id, node_provider,
        explicit_treenode_ids=tuple(), explicit_connector_ids=tuple(),
        include_labels=False, target_format='json', target_options=None,
        with_relation_map=True):

    result_tuple, data_type = node_provider.get_tuples(params, project_id,
        explicit_treenode_ids, explicit_connector_ids, include_labels,
        with_relation_map)

    return create_node_response(result_tuple, params, target_format, target_options, data_type)

def compile_node_list_result(project_id, node_providers, params,
        explicit_treenode_ids=tuple(), explicit_connector_ids=tuple(),
        include_labels=False, target_format='json', target_options=None,
        with_relation_map=True):
    """Create a valid HTTP response for the provided node query. If
    override_provider is not passed in, the list of node_providers will be
    iterated until a result is found.
    """
    result_tuple, data_type = None, None
    for node_provider in node_providers:
        if node_provider.matches(params):
            result = node_provider.get_tuples(params, project_id,
                explicit_treenode_ids, explicit_connector_ids, include_labels,
                with_relation_map)
            result_tuple, data_type =  result

            if result_tuple and data_type:
                break

    if not (result_tuple and data_type):
        raise ValueError("Could not find matching node provider for request")

    return create_node_response(result_tuple, params, target_format, target_options, data_type)

def create_node_response(result, params, target_format, target_options, data_type):
    if target_format == 'json':
        if data_type == 'json':
            data = ujson.dumps(result)
        elif data_type == 'json_text':
            data = result
        elif data_type == 'msgpack':
            data = ujson.dumps(msgpack.unpackb(result, use_list=False))
        else:
            raise ValueError("Unknown data type: " + data_type)
        return HttpResponse(data, content_type='application/json')
    elif target_format == 'msgpack':
        if data_type == 'json':
            data = msgpack.packb(result)
        elif data_type == 'json_text':
            data = msgpack.packb(ujson.loads(result))
        elif data_type == 'msgpack':
            data = result
        else:
            raise ValueError("Unknown data type: " + data_type)
        return HttpResponse(data, content_type='application/octet-stream')
    elif target_format == 'png' or target_format == 'gif':
        if data_type == 'json':
            data = result
        elif data_type == 'json_text':
            data = ujson.loads(result)
        elif data_type == 'msgpack':
            data = msgpack.unpackb(result, use_list=False)
        else:
            raise ValueError("Unknown data type: " + data_type)
        width = target_options['view_width']
        height = target_options['view_height']
        view_min_x = params['left']
        view_min_y = params['top']
        xscale = width / (params['right'] - params['left'])
        yscale = height / (params['bottom'] - params['top'])
        image = render_nodes_xy(data, params, width, height, view_min_x,
                view_min_y, xscale, yscale)
        # serialize to HTTP response
        if target_format == 'png':
            response = HttpResponse(content_type="image/png")
            image.save(response, "PNG")
        else:
            response = HttpResponse(content_type="image/gif")
            image.save(response, 'GIF', transparency=0, optimize=True)
        return response
    else:
        raise ValueError("Unknown target format: {}".format(target_format))

def render_nodes_xy(node_data, params, width, height, view_min_x=0, view_min_y=0,
        xscale=1.0, yscale=1.0):
    """Render the passed in node data to an image.
    """
    import random
    background = (255, 0, 0, 0)
    image = Image.new('RGBA', (width, height), background)

    radius = 4
    hr = radius / 2.0
    node_pen = Pen((255, 0, 255), 1)
    root_pen = Pen('red', 1)
    #leaf_pen = Pen('red', 1)
    node_brush = Brush((255, 0, 255))
    root_brush = Brush('red')
    #leaf_brush = Brush('red')

    virtual_nodes = True

    left, right = params['left'], params['right']
    top, bottom = params['top'], params['bottom']
    z1, z2 = params['z1'], params['z2']

    # Map parents to children
    nodes = dict()
    treenodes = node_data[0]
    for tn in treenodes:
        nodes[tn[0]] = tn

    draw = Draw(image)
    # Add treenodes:
    for tn in treenodes:
        parent_id = tn[1]
        x, y, z = tn[2], tn[3], tn[4]
        xs, ys = None, None
        virtual_node_created = False

        is_outside = x < left or x >= right or y < top \
                or y >= bottom or z < z1 or z >= z2

        # If this node and its parent are outside, create a virtual node
        # location and replace current coordinates.
        if is_outside:
            if virtual_nodes and parent_id:
                parent_node = nodes.get(parent_id)
                if parent_node:
                    px, py, pz = parent_node[2], parent_node[3], parent_node[4]
                    p_is_outside = px < left or px >= right or py < top \
                            or py >= bottom or pz < z1 or pz >= z2

                    # If the parent ist outside, too, find virtual node
                    # location.
                    if p_is_outside:
                        dx, dy, dz = px - x, py - y, pz - z
                        if dz > -0.0001 and dz < 0.0001:
                            continue
                        t = (z1 - z) / dz

                        x = x + t * dx
                        y = y + t * dy
                        z = z1
                        virtual_node_created = True

            if not virtual_node_created:
                continue

        xs = xscale * (x - view_min_x)
        ys = yscale * (y - view_min_y)

        if parent_id:
            pen = node_pen
            brush = node_brush
        else:
            pen = root_pen
            brush = root_brush

        # Render node or virtual node
        draw.ellipse((xs - hr, ys - hr, xs + hr, ys + hr), pen, brush)

    draw.flush()

    return image


@requires_user_role(UserRole.Annotate)
def update_location_reviewer(request, project_id=None, node_id=None):
    """ Updates the reviewer id and review time of a node """
    try:
        # Try to get the review object. If this fails we create a new one. Doing
        # it in a try/except instead of get_or_create allows us to retrieve the
        # skeleton ID only if needed.
        r = Review.objects.get(treenode_id=node_id, reviewer=request.user)
    except Review.DoesNotExist:
        node = get_object_or_404(Treenode, pk=node_id)
        r = Review(project_id=project_id, treenode_id=node_id,
                   skeleton_id=node.skeleton_id, reviewer=request.user)

    r.review_time = timezone.now()
    r.save()

    return JsonResponse({
        'reviewer_id': request.user.id,
        'review_time': r.review_time.isoformat(),
    })


@requires_user_role([UserRole.Annotate, UserRole.Browse])
def most_recent_treenode(request, project_id=None):
    skeleton_id = int(request.POST.get('skeleton_id', -1))

    try:
        # Select the most recently edited node
        tn = Treenode.objects.filter(project=project_id, editor=request.user)
        if not skeleton_id == -1:
            tn = tn.filter(skeleton=skeleton_id)
        tn = tn.extra(select={'most_recent': 'greatest(treenode.creation_time, treenode.edition_time)'})\
             .extra(order_by=['-most_recent', '-treenode.id'])[0] # [0] generates a LIMIT 1
    except IndexError:
        # No treenode edited by the user exists in this skeleton.
        return JsonResponse({})

    return JsonResponse({
        'id': tn.id,
        #'skeleton_id': tn.skeleton.id,
        'x': int(tn.location_x),
        'y': int(tn.location_y),
        'z': int(tn.location_z),
        #'most_recent': str(tn.most_recent) + tn.most_recent.strftime('%z'),
        #'most_recent': tn.most_recent.strftime('%Y-%m-%d %H:%M:%S.%f'),
        #'type': 'treenode'
    })


def _update_location(table, nodes, now, user, cursor):
    if not nodes:
        return
    # 0: id
    # 1: X
    # 2: Y
    # 3: Z
    can_edit_all_or_fail(user, (node[0] for node in nodes), table)

    # Sanitize node details
    nodes = [(int(i), float(x), float(y), float(z)) for i,x,y,z in nodes]

    node_template = "(" + "),(".join(["%s, %s, %s, %s"] * len(nodes)) + ")"
    node_table = [v for k in nodes for v in k]

    # Sanitize table name, because we are going to insert it directly into the
    # SQL query below.
    if table not in ('treenode', 'connector'):
        raise ValueError('Expected treenode or connector')

    # If we update the location parent table to update both treenode and connector
    # nodes, statement level triggers would not be run on the respective child
    # tables (treenode and connector).
    cursor.execute("""
        UPDATE {} n
        SET editor_id = %s, location_x = target.x,
            location_y = target.y, location_z = target.z
        FROM (SELECT x.id, x.location_x AS old_loc_x,
                     x.location_y AS old_loc_y, x.location_z AS old_loc_z,
                     y.new_loc_x AS x, y.new_loc_y AS y, y.new_loc_z AS z
              FROM {} x
              INNER JOIN (VALUES {}) y(id, new_loc_x, new_loc_y, new_loc_z)
              ON x.id = y.id FOR NO KEY UPDATE) target
        WHERE n.id = target.id
        RETURNING n.id, n.edition_time, target.old_loc_x, target.old_loc_y,
                  target.old_loc_z
    """.format(table, table, node_template), [user.id] + node_table)

    updated_rows = cursor.fetchall()
    if len(nodes) != len(updated_rows):
        raise ValueError('Coudn\'t update node ' +
                         ','.join(frozenset([str(r[0]) for r in nodes]) -
                                  frozenset([str(r[0]) for r in updated_rows])))
    return updated_rows


@requires_user_role(UserRole.Annotate)
def node_update(request, project_id=None):
    treenodes = get_request_list(request.POST, "t") or []
    connectors = get_request_list(request.POST, "c") or []

    cursor = connection.cursor()
    nodes = treenodes + connectors
    if nodes:
        node_ids = [int(n[0]) for n in nodes]
        state.validate_state(node_ids, request.POST.get('state'),
                multinode=True, lock=True, cursor=cursor)

    now = timezone.now()
    old_treenodes = _update_location("treenode", treenodes, now, request.user, cursor)
    old_connectors = _update_location("connector", connectors, now, request.user, cursor)

    num_updated_nodes = len(treenodes) + len(connectors)
    return JsonResponse({
        'updated': num_updated_nodes,
        'old_treenodes': old_treenodes,
        'old_connectors': old_connectors
    })


@requires_user_role([UserRole.Annotate, UserRole.Browse])
def node_nearest(request, project_id=None):
    params = {}
    param_float_defaults = {
        'x': 0,
        'y': 0,
        'z': 0}
    param_int_defaults = {
        'skeleton_id': -1,
        'neuron_id': -1}
    for p in param_float_defaults.keys():
        params[p] = float(request.POST.get(p, param_float_defaults[p]))
    for p in param_int_defaults.keys():
        params[p] = int(request.POST.get(p, param_int_defaults[p]))
    relation_map = get_relation_to_id_map(project_id)

    if params['skeleton_id'] < 0 and params['neuron_id'] < 0:
        raise Exception('You must specify either a skeleton or a neuron')

    for rel in ['part_of', 'model_of']:
        if rel not in relation_map:
            raise Exception('Could not find required relation %s for project %s.' % (rel, project_id))

    skeletons = []
    if params['skeleton_id'] > 0:
        skeletons.append(params['skeleton_id'])

    response_on_error = ''
    try:
        if params['neuron_id'] > 0:  # Add skeletons related to specified neuron
            # Assumes that a cici 'model_of' relationship always involves a
            # skeleton as ci_a and a neuron as ci_b.
            response_on_error = 'Finding the skeletons failed.'
            neuron_skeletons = ClassInstanceClassInstance.objects.filter(
                class_instance_b=params['neuron_id'],
                relation=relation_map['model_of'])
            for neur_skel_relation in neuron_skeletons:
                skeletons.append(neur_skel_relation.class_instance_a_id)

        # Get all treenodes connected to skeletons
        response_on_error = 'Finding the treenodes failed.'
        treenodes = Treenode.objects.filter(project=project_id, skeleton__in=skeletons)

        def getNearestTreenode(x, y, z, treenodes):
            minDistance = -1
            nearestTreenode = None
            for tn in treenodes:
                xdiff = x - tn.location_x
                ydiff = y - tn.location_y
                zdiff = z - tn.location_z
                distanceSquared = xdiff ** 2 + ydiff ** 2 + zdiff ** 2
                if distanceSquared < minDistance or minDistance < 0:
                    nearestTreenode = tn
                    minDistance = distanceSquared
            return nearestTreenode

        nearestTreenode = getNearestTreenode(
            params['x'],
            params['y'],
            params['z'],
            treenodes)
        if nearestTreenode is None:
            raise Exception('No treenodes were found for skeletons in %s' % skeletons)

        return JsonResponse({
            'treenode_id': nearestTreenode.id,
            'x': int(nearestTreenode.location_x),
            'y': int(nearestTreenode.location_y),
            'z': int(nearestTreenode.location_z),
            'skeleton_id': nearestTreenode.skeleton_id})

    except Exception as e:
        raise Exception(response_on_error + ':' + str(e))


def _fetch_location(project_id, location_id):
    """Get the locations of the passed in node ID in the passed in project."""
    locations = _fetch_locations(project_id, [location_id])
    if not locations:
        raise ValueError('Could not find location for node {}'.format(location_id))
    return locations[0]


def _fetch_locations(project_id, location_ids):
    """Get the locations of the passed in node IDs in the passed in project."""
    node_template = ",".join("(%s)" for _ in location_ids)
    params = list(location_ids)
    params.append(project_id)
    cursor = connection.cursor()
    cursor.execute('''
        SELECT
          l.id,
          l.location_x AS x,
          l.location_y AS y,
          l.location_z AS z
        FROM location l
        JOIN (VALUES {}) node(id)
            ON l.id = node.id
        WHERE project_id = %s
    '''.format(node_template), params)
    return cursor.fetchall()

@requires_user_role([UserRole.Annotate, UserRole.Browse])
def get_location(request, project_id=None):
    tnid = int(request.POST['tnid'])
    return JsonResponse(_fetch_location(project_id, tnid), safe=False)

@api_view(['POST'])
@requires_user_role([UserRole.Browse])
def get_locations(request, project_id=None):
    """Get locations for a particular set of nodes in a project.

    A list of lists is returned. Each inner list represents one location and
    hast the following format: [id, x, y, z].
    ---
    parameters:
        - name: node_ids
          description: A list of node IDs to get the location for
          required: true
          type: array
          items:
            type: number
            format: integer
          required: true
          paramType: form
    models:
      location_element:
        id: location_element
        properties:
        - name: id
          description: ID of the node.
          type: integer
          required: true
        - name: x
          description: X coordinate of the node.
          required: true
          type: number
          format: double
          paramType: form
        - name: y
          description: Y coordinate of the node.
          required: true
          type: number
          format: double
          paramType: form
        - name: z
          description: Z coordinate of the node.
          required: true
          type: number
          format: double
          paramType: form
    type:
    - type: array
      items:
        $ref: location_element
      required: true
    """
    node_ids = get_request_list(request.POST, 'node_ids', map_fn=int)
    locations = _fetch_locations(project_id, node_ids)
    return JsonResponse(locations, safe=False)


@requires_user_role([UserRole.Browse])
def user_info(request, project_id=None):
    """Return information on a treenode or connector. This function is called
    pretty often (with every node activation) and should therefore be as fast
    as possible.
    """
    node_ids = get_request_list(request.POST, 'node_ids', map_fn=int)
    if not node_ids:
        raise ValueError('Need at least one node ID')

    node_template = ','.join('(%s)' for n in node_ids)

    cursor = connection.cursor()
    cursor.execute('''
        SELECT n.id, n.user_id, n.editor_id, n.creation_time, n.edition_time,
               array_agg(r.reviewer_id), array_agg(r.review_time)
        FROM location n
        JOIN (VALUES {}) req_node(id)
            ON n.id = req_node.id
        LEFT OUTER JOIN review r
            ON r.treenode_id = n.id
        GROUP BY n.id
    '''.format(node_template), node_ids)

    # Build result
    result = {}
    for row in cursor.fetchall():
        result[row[0]] = {
            'user': row[1],
            'editor': row[2],
            'creation_time': str(row[3].isoformat()),
            'edition_time': str(row[4].isoformat()),
            'reviewers': [r for r in row[5] if r],
            'review_times': [str(rt.isoformat()) for rt in row[6] if rt]
        }

    return JsonResponse(result)

@api_view(['POST'])
@requires_user_role([UserRole.Browse])
def find_labels(request, project_id=None):
    """List nodes with labels matching a query, ordered by distance.

    Find nodes with labels (front-end node tags) matching a regular
    expression, sort them by ascending distance from a reference location, and
    return the result. Returns at most 50 nodes.
    ---
    parameters:
        - name: x
          description: X coordinate of the distance reference in project space.
          required: true
          type: number
          format: double
          paramType: form
        - name: y
          description: Y coordinate of the distance reference in project space.
          required: true
          type: number
          format: double
          paramType: form
        - name: z
          description: Z coordinate of the distance reference in project space.
          required: true
          type: number
          format: double
          paramType: form
        - name: label_regex
          description: Regular expression query to match labels
          required: true
          type: string
          paramType: form
    models:
      find_labels_node:
        id: find_labels_node
        properties:
        - description: ID of a node with a matching label
          type: integer
          required: true
        - description: Node location
          type: array
          items:
            type: number
            format: double
          required: true
        - description: |
            Euclidean distance from the reference location in project space
          type: number
          format: double
          required: true
        - description: Labels on this node matching the query
          type: array
          items:
            type: string
          required: true
    type:
    - type: array
      items:
        $ref: find_labels_node
      required: true
    """
    x = float(request.POST['x'])
    y = float(request.POST['y'])
    z = float(request.POST['z'])
    label_regex = str(request.POST['label_regex'])

    cursor = connection.cursor()
    cursor.execute("""
            (SELECT
                n.id,
                n.location_x,
                n.location_y,
                n.location_z,
                SQRT(POW(n.location_x - %s, 2)
                   + POW(n.location_y - %s, 2)
                   + POW(n.location_z - %s, 2)) AS dist,
                ARRAY_TO_JSON(ARRAY_AGG(l.name)) AS labels
            FROM treenode n, class_instance l, treenode_class_instance nl, relation r
            WHERE r.id = nl.relation_id
              AND r.relation_name = 'labeled_as'
              AND nl.treenode_id = n.id
              AND l.id = nl.class_instance_id
              AND n.project_id = %s
              AND l.name ~ %s
            GROUP BY n.id)

            UNION ALL

            (SELECT
                n.id,
                n.location_x,
                n.location_y,
                n.location_z,
                SQRT(POW(n.location_x - %s, 2)
                   + POW(n.location_y - %s, 2)
                   + POW(n.location_z - %s, 2)) AS dist,
                ARRAY_TO_JSON(ARRAY_AGG(l.name)) AS labels
            FROM connector n, class_instance l, connector_class_instance nl, relation r
            WHERE r.id = nl.relation_id
              AND r.relation_name = 'labeled_as'
              AND nl.connector_id = n.id
              AND l.id = nl.class_instance_id
              AND n.project_id = %s
              AND l.name ~ %s
            GROUP BY n.id)

            ORDER BY dist
            LIMIT 50
            """, (x, y, z, project_id, label_regex,
                  x, y, z, project_id, label_regex,))

    return JsonResponse([
            [row[0],
             [row[1], row[2], row[3]],
             row[4],
             row[5]] for row in cursor.fetchall()], safe=False)
