# Copyright Contributors to the Amundsen project.
# SPDX-License-Identifier: Apache-2.0

import logging
import json
from typing import (
    Any, Dict, List, Union,
)

from amundsen_common.models.api import health_check
from amundsen_common.models.search import Filter, SearchResponse
from elasticsearch import Elasticsearch
from elasticsearch.exceptions import ConnectionError as ElasticConnectionError, ElasticsearchException
from elasticsearch_dsl import (
    MultiSearch, Q, Search,
)
from elasticsearch_dsl.query import Match, RankFeature
from elasticsearch_dsl.response import Response
from elasticsearch_dsl.utils import AttrDict, AttrList
from werkzeug.exceptions import InternalServerError

from search_service.proxy.es_proxy_utils import Resource

LOGGER = logging.getLogger(__name__)

# ES query constants

BOOL_QUERY = 'bool'
WILDCARD_QUERY = 'wildcard'
TERM_QUERY = 'term'
TERMS_QUERY = 'terms'

DEFAULT_FUZZINESS = "AUTO"


class ElasticsearchProxy():
    PRIMARY_ENTITIES = [Resource.TABLE, Resource.DASHBOARD, Resource.FEATURE, Resource.USER]

    # map the field name in FE to the field used to filter in ES
    # note: ES needs keyword field types to filter

    GENERAL_MAPPING = {
        'key': 'key',
        'description': 'description',
        'resource_type': 'resource_type',
    }

    TABLE_MAPPING = {
        **GENERAL_MAPPING,
        'badges': 'badges.keyword',
        'tag': 'tags.keyword',
        'schema': 'schema.keyword',
        'table': 'name.keyword',
        'column': 'columns.keyword',
        'database': 'database.keyword',
        'cluster': 'cluster.keyword',
    }

    DASHBOARD_MAPPING = {
        **GENERAL_MAPPING,
        'url': 'url',
        'uri': 'uri',
        'last_successful_run_timestamp': 'last_successful_run_timestamp',
        'group_name': 'group_name.keyword',
        'chart_names': 'chart_names.keyword',
        'query_names': 'query_names.keyword',
        'name': 'name.keyword',
        'tag': 'tags.keyword',
    }

    FEATURE_MAPPING = {
        **GENERAL_MAPPING,
        'version': 'version',
        'availability': 'availability',
        'feature_group': 'feature_group.keyword',
        'feature_name': 'name.keyword',
        'entity': 'entity.keyword',
        'status': 'status.keyword',
        'tags': 'tags.keyword',
        'badges': 'badges.keyword',
    }

    USER_MAPPING = {
        'full_name': 'name.keyword',
        'email': 'key',
        'first_name': 'first_name',
        'last_name': 'last_name',
        'resource_type': 'resource_type',
    }

    RESOUCE_TO_MAPPING = {
        Resource.TABLE: TABLE_MAPPING,
        Resource.DASHBOARD: DASHBOARD_MAPPING,
        Resource.FEATURE: FEATURE_MAPPING,
        Resource.USER: USER_MAPPING,
    }

    def __init__(self, *,
                 host: str = None,
                 user: str = '',
                 password: str = '',
                 client: Elasticsearch = None,
                 page_size: int = 10) -> None:
        if client:
            self.elasticsearch = client
        else:
            http_auth = (user, password) if user else None
            self.elasticsearch = Elasticsearch(host, http_auth=http_auth)
        self.page_size = page_size

    def health(self) -> health_check.HealthCheck:
        """
        Returns the health of the Elastic search cluster
        """
        try:
            if self.elasticsearch.ping():
                health = self.elasticsearch.cluster.health()
                # ES status vaues: green, yellow, red
                status = health_check.OK if health['status'] != 'red' else health_check.FAIL
            else:
                health = {'status': 'Unable to connect'}
                status = health_check.FAIL
            checks = {f'{type(self).__name__}:connection': health}
        except ElasticConnectionError:
            status = health_check.FAIL
            checks = {f'{type(self).__name__}:connection': {'status': 'Unable to connect'}}
        return health_check.HealthCheck(status=status, checks=checks)

    def get_index_for_resource(self, resource_type: Resource) -> str:
        resource_str = resource_type.name.lower()
        return f"{resource_str}_search_index"

    def _build_must_query(self, resource: Resource, query_term: str) -> List[Q]:
        """
        Builds the query object for the inputed search term
        """

        if not query_term:
            # We don't want to create match query for ""
            # because it will result in no matches even with filters
            return []

        # query for fields general to all resources
        should_clauses: List[Q] = [
            Match(name={
                "query": query_term,
                "fuzziness": DEFAULT_FUZZINESS,
                "max_expansions": 10,
                "boost": 5
            }),
            Match(description={
                "query": query_term,
                "fuzziness": DEFAULT_FUZZINESS,
                "max_expansions": 10,
                "boost": 1.5
            }),
            Match(badges={
                "query": query_term,
                "fuzziness": DEFAULT_FUZZINESS,
                "max_expansions": 10
            }),
            Match(tags={
                "query": query_term,
                "fuzziness": DEFAULT_FUZZINESS,
                "max_expansions": 10
            }),
        ]

        if resource == Resource.TABLE:
            should_clauses.extend([
                Match(schema={
                    "query": query_term,
                    "fuzziness": DEFAULT_FUZZINESS,
                    "max_expansions": 10,
                    "boost": 3
                }),
                Match(columns={
                    "query": query_term,
                    "fuzziness": DEFAULT_FUZZINESS,
                    "boost": 2,
                    "max_expansions": 5
                }),
            ])
        elif resource == Resource.DASHBOARD:
            should_clauses.extend([
                Match(group_name={
                    "query": query_term,
                    "fuzziness": DEFAULT_FUZZINESS,
                    "max_expansions": 10,
                    "boost": 3
                }),
                Match(query_names={
                    "query": query_term,
                    "fuzziness": DEFAULT_FUZZINESS,
                    "max_expansions": 10,
                    "boost": 2
                }),
                Match(chart_names={
                    "query": query_term,
                    "fuzziness": DEFAULT_FUZZINESS,
                    "max_expansions": 10,
                    "boost": 2
                }),
                Match(uri={
                    "query": query_term,
                    "fuzziness": DEFAULT_FUZZINESS,
                    "max_expansions": 10,
                    "boost": 4
                }),
            ])
        elif resource == Resource.FEATURE:
            should_clauses.extend([
                Match(feature_group={
                    "query": query_term,
                    "fuzziness": DEFAULT_FUZZINESS,
                    "max_expansions": 10,
                    "boost": 3
                }),
                Match(version={
                    "query": query_term
                }),
                Match(entity={
                    "query": query_term,
                    "fuzziness": DEFAULT_FUZZINESS,
                    "max_expansions": 10,
                    "boost": 2
                }),
                Match(status={
                    "query": query_term
                }),
            ])
        elif resource == Resource.USER:
            # replaces rather than extending
            should_clauses = [
                Match(name={
                    "query": query_term,
                    "fuzziness": DEFAULT_FUZZINESS,
                    "max_expansions": 10,
                    "boost": 5
                }),
                Match(first_name={
                    "query": query_term,
                    "fuzziness": DEFAULT_FUZZINESS,
                    "max_expansions": 10,
                    "boost": 3
                }),
                Match(last_name={
                    "query": query_term,
                    "fuzziness": DEFAULT_FUZZINESS,
                    "max_expansions": 10,
                    "boost": 3
                }),
                Match(team_name={
                    "query": query_term,
                    "fuzziness": DEFAULT_FUZZINESS,
                    "max_expansions": 10
                }),
                Match(key={
                    "query": query_term,
                    "fuzziness": DEFAULT_FUZZINESS,
                    "max_expansions": 10,
                    "boost": 4
                }),
            ]

        must_clauses: List[Q] = [Q(BOOL_QUERY, should=should_clauses)]

        return must_clauses

    def _build_should_query(self, resource: Resource, query_term: str) -> List[Q]:

        # no scoring happens if there is no search term
        if query_term == '':
            return []

        # general usage metric for searcheable resources
        usage_metric_fields = {
            'total_usage': 10.0,
        }

        if resource == Resource.TABLE:
            usage_metric_fields = {
                **usage_metric_fields,
                'unique_usage': 10.0,
            }
        if resource == Resource.USER:
            usage_metric_fields = {
                'total_read': 10.0,
                'total_own': 10.0,
                'total_follow': 10.0,
            }

        rank_feature_queries = []

        for metric in usage_metric_fields.keys():
            field_name = f'usage.{metric}'
            boost = usage_metric_fields[metric]
            rank_feature_query = RankFeature(field=field_name,
                                             boost=boost)
            rank_feature_queries.append(rank_feature_query)

        return rank_feature_queries

    def _build_filters(self, resource: Resource, filters: List[Filter]) -> List:
        """
        Builds the query object for all of the filters given in the search request
        """
        mapping = self.RESOUCE_TO_MAPPING.get(resource)

        filter_queries: List = []

        for filter in filters:
            filter_name = mapping.get(filter.name) if mapping is not None \
                and mapping.get(filter.name) is not None else filter.name

            queries_per_term = [Q(WILDCARD_QUERY, **{filter_name: term}) for term in filter.values]

            if filter.operation == 'OR':
                filter_queries.append(Q(BOOL_QUERY, should=queries_per_term, minimum_should_match=1))

            elif filter.operation == 'AND':
                for q in queries_per_term:
                    filter_queries.append(q)

            else:
                msg = f"Invalid operation {filter.operation} for filter {filter_name} with values {filter.values}"
                raise ValueError(msg)

        return filter_queries

    def _build_elasticsearch_query(self, *,
                                   resource: Resource,
                                   query_term: str,
                                   filters: List[Filter]) -> Q:

        must_query = self._build_must_query(resource=resource,
                                            query_term=query_term)

        should_query = self._build_should_query(resource=resource,
                                                query_term=query_term)

        filters = self._build_filters(resource=resource, filters=filters)

        es_query = Q(BOOL_QUERY, must=must_query, should=should_query, filter=filters)

        return es_query

    def _format_response(self, page_index: int,
                         results_per_page: int,
                         responses: List[Response],
                         resource_types: List[Resource]) -> SearchResponse:
        resource_types_str = [r.name.lower() for r in resource_types]
        no_results_for_resource = {
            "results": [],
            "total_results": 0
        }
        results_per_resource = {resource: no_results_for_resource for resource in resource_types_str}

        for r in responses:
            if r.success():
                if len(r.hits.hits) > 0:
                    resource_type = r.hits.hits[0]._source['resource_type']
                    results = []
                    for search_result in r.hits.hits:
                        # mapping gives all the fields in the response
                        result = {}
                        fields = self.RESOUCE_TO_MAPPING[Resource[resource_type.upper()]]
                        for f in fields.keys():
                            # remove "keyword" from mapping value
                            field = fields[f].split('.')[0]
                            try:
                                result_for_field = search_result._source[field]
                                # AttrList and AttrDict are not json serializable
                                if type(result_for_field) is AttrList:
                                    result_for_field = list(result_for_field)
                                elif type(result_for_field) is AttrDict:
                                    result_for_field = result_for_field.to_dict()
                                result[f] = result_for_field
                            except KeyError:
                                logging.debug(f'Field: {field} missing in search response.')
                                pass
                        result["search_score"] = search_result._score
                        results.append(result)
                    # replace empty results with actual results
                    results_per_resource[resource_type] = {
                        "results": results,
                        "total_results": r.hits.total.value
                    }
            else:
                raise InternalServerError(f"Request to Elasticsearch failed: {r.failures}")

        return SearchResponse(msg="Success",
                              page_index=page_index,
                              results_per_page=results_per_page,
                              results=results_per_resource,
                              status_code=200)

    def execute_queries(self, queries: Dict[Resource, Q],
                        page_index: int,
                        results_per_page: int) -> List[Response]:
        multisearch = MultiSearch(using=self.elasticsearch)

        for resource in queries.keys():
            query_for_resource = queries.get(resource)
            search = Search(index=self.get_index_for_resource(resource_type=resource)).query(query_for_resource)

            # pagination
            start_from = page_index * results_per_page
            end = results_per_page * (page_index + 1)

            search = search[start_from:end]

            multisearch = multisearch.add(search)
        try:
            logging.info(json.dumps(multisearch.to_dict()))
            response = multisearch.execute()
            return response
        except Exception as e:
            LOGGER.error(f'Failed to execute ES search queries. {e}')
            return []

    def search(self, *,
               query_term: str,
               page_index: int,
               results_per_page: int,
               resource_types: List[Resource],
               filters: List[Filter]) -> SearchResponse:
        if resource_types == []:
            # if resource types are not defined then search all resources
            resource_types = self.PRIMARY_ENTITIES

        queries: Dict[Resource, Q] = {}
        for resource in resource_types:
            # build a query for each resource to search
            queries[resource] = self._build_elasticsearch_query(resource=resource,
                                                                query_term=query_term,
                                                                filters=filters)

        responses = self.execute_queries(queries=queries,
                                         page_index=page_index,
                                         results_per_page=results_per_page)

        formatted_response = self._format_response(page_index=page_index,
                                                   results_per_page=results_per_page,
                                                   responses=responses,
                                                   resource_types=resource_types)

        return formatted_response

    def get_document_json_by_key(self,
                                 resource_key: str,
                                 resource_type: Resource) -> Any:
        key_query = {
            resource_type: Q(TERM_QUERY, key=resource_key),
        }
        response: Response = self.execute_queries(queries=key_query,
                                                  page_index=0,
                                                  results_per_page=1)
        if len(response) < 1:
            msg = f'No response from ES for key query {key_query[resource_type]}'
            LOGGER.error(msg)
            raise ElasticsearchException(msg)

        response = response[0]
        if response.success():
            results_count = response.hits.total.value
            if results_count == 1:
                es_result = response.hits.hits[0]
                return es_result

            if results_count > 1:
                msg = f'Key {key_query[resource_type]} is not unique to a single ES resource'
                LOGGER.error(msg)
                raise ValueError(msg)

            else:
                # no doc exists with given key in ES
                msg = f"Requested key {resource_key} query returned no results in ES"
                LOGGER.error(msg)
                raise ValueError(msg)
        else:
            msg = f'Request to Elasticsearch failed: {response.failures}'
            LOGGER.error(msg)
            raise InternalServerError(msg)

    def update_document_by_id(self, *,
                              resource_type: Resource,
                              field: str,
                              new_value: Union[List, str, None],
                              document_id: str) -> None:

        partial_document = {
            "doc": {
                field: new_value
            }
        }
        self.elasticsearch.update(index=self.get_index_for_resource(resource_type=resource_type),
                                  id=document_id,
                                  body=partial_document)

    def update_document_by_key(self, *,
                               resource_key: str,
                               resource_type: Resource,
                               field: str,
                               value: str = None,
                               operation: str = 'add') -> str:

        mapped_field = self.RESOUCE_TO_MAPPING[resource_type].get(field)
        if not mapped_field:
            mapped_field = field

        try:
            es_hit = self.get_document_json_by_key(resource_key=resource_key,
                                                   resource_type=resource_type)
            document_id = es_hit._id
            current_value = getattr(es_hit._source, mapped_field)

        except Exception as e:
            msg = f'Failed to get ES document id and current value for key {resource_key}. {e}'
            LOGGER.error(msg)
            return msg

        new_value = current_value

        if operation == 'overwrite':
            if type(current_value) is AttrList:
                new_value = [value]
            else:
                new_value = value
        else:
            # operation is add
            if type(current_value) is AttrList:
                curr_list = list(current_value)
                curr_list.append(value)
                new_value = curr_list
            else:
                new_value = [current_value, value]

        try:
            self.update_document_by_id(resource_type=resource_type,
                                       field=mapped_field,
                                       new_value=new_value,
                                       document_id=document_id)
        except Exception as e:
            msg = f'Failed to update field {field} with value {new_value} for {resource_key}. {e}'
            LOGGER.error(msg)
            return msg

        return f'ES document field {field} for {resource_key} with value {value} was updated successfully'

    def delete_document_by_key(self, *,
                               resource_key: str,
                               resource_type: Resource,
                               field: str,
                               value: str = None) -> str:
        mapped_field = self.RESOUCE_TO_MAPPING[resource_type].get(field)
        if not mapped_field:
            mapped_field = field

        try:
            es_hit = self.get_document_json_by_key(resource_key=resource_key,
                                                   resource_type=resource_type)
            document_id = es_hit._id
            current_value = getattr(es_hit._source, mapped_field)

        except Exception as e:
            msg = f'Failed to get ES document id and current value for key {resource_key}. {e}'
            LOGGER.error(msg)
            return msg

        new_value = current_value

        if type(current_value) is AttrList:
            if value:
                curr_list = list(current_value)
                curr_list.remove(value)
                new_value = curr_list
            else:
                new_value = []
        else:
            # no value given when deleting implies
            # delete is happening on a single value field
            new_value = ""
        try:
            self.update_document_by_id(resource_type=resource_type,
                                       field=mapped_field,
                                       new_value=new_value,
                                       document_id=document_id)
        except Exception as e:
            msg = f'Failed to delete field {field} with value {new_value} for {resource_key}. {e}'
            LOGGER.error(msg)
            return msg

        return f'ES document field {field} for {resource_key} with value {value} was deleted successfully'
