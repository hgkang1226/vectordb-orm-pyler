import pinecone
from vectordb_orm.backends.base import BackendBase
from typing import Type
from vectordb_orm.base import VectorSchemaBase
import numpy as np
from vectordb_orm.attributes import AttributeCompare
from vectordb_orm.fields import EmbeddingField
from re import match as re_match
from logging import info
from uuid import uuid4
from vectordb_orm.results import QueryResult

class PineconeBackend(BackendBase):
    max_fetch_size = 1000

    def __init__(
        self,
        api_key: str,
        environment: str
    ):
        pinecone.init(
            api_key=api_key,
            environment=environment,
        )

    def create_collection(self, schema: Type[VectorSchemaBase]):
        collection_name = self.transform_collection_name(schema.collection_name())
        current_indexes = pinecone.list_indexes()
        if collection_name in current_indexes:
            info("Collection already created...")
            return

        info("Creating collection, this could take 30s-5min...")

        self._assert_valid_collection_name(collection_name)
        self._assert_has_primary(schema)

        # Pinecone allows for dynamic keys on each object
        # However we need to pre-provide the keys we want to search on
        # This does increase memory size so we can consider adding an explict index flag
        # for the metadata fields that we want to allow search on
        # https://docs.pinecone.io/docs/manage-indexes#selective-metadata-indexing
        metadata_config = {
            "indexed": [
                key
                for key in schema.__annotations__.keys()
            ]
        }

        _, embedding_field = self._get_embedding_field(schema)

        pinecone.create_index(
            name=collection_name,
            dimension=embedding_field.dim,
            metric="euclidean",
            metadata_config=metadata_config
        )
        return pinecone.Index(index_name=collection_name)

    def clear_collection(self, schema: Type[VectorSchemaBase]):
        collection_name = self.transform_collection_name(schema.collection_name())

        current_indexes = pinecone.list_indexes()
        if collection_name not in current_indexes:
            return

        index = pinecone.Index(index_name=collection_name)
        delete_response = index.delete(delete_all=True)
        if delete_response:
            # Success should have an empty dict
            raise ValueError(f"Failed to clear collection {collection_name}: {delete_response}")

    def delete_collection(self, schema: Type[VectorSchemaBase]):
        collection_name = self.transform_collection_name(schema.collection_name())
    
        # Pinecone throws a 404 if the index doesn't exist
        current_indexes = pinecone.list_indexes()
        if collection_name not in current_indexes:
            return

        pinecone.delete_index(collection_name)

    def insert(self, entity: VectorSchemaBase) -> list:
        schema = entity.__class__
        collection_name = self.transform_collection_name(schema.collection_name())

        schema = entity.__class__
        embedding_field_key, _ = self._get_embedding_field(schema)
        primary_key = self._get_primary(schema)

        id = uuid4().int & (1<<64)-1

        embedding_value : np.ndarray = getattr(entity, embedding_field_key)
        metadata_fields = {
            key: getattr(entity, key)
            for key in schema.__annotations__.keys()
            if key not in {embedding_field_key, primary_key}
        }

        index = pinecone.Index(index_name=collection_name)
        index.upsert([
            (
                str(id),
                embedding_value.tolist(),
                {
                    **metadata_fields,
                    primary_key: id
                }
            ),
        ])

        return id

    def delete(self, entity: VectorSchemaBase):
        schema = entity.__class__
        collection_name = self.transform_collection_name(schema.collection_name())
        index = pinecone.Index(index_name=collection_name)
        delete_response = index.delete(ids=[str(entity.id)])
        if delete_response:
            # Success should have an empty dict
            raise ValueError(f"Failed to clear collection {collection_name}: {delete_response}")

    def search(
        self,
        schema: Type[VectorSchemaBase],
        output_fields: list[str],
        filters: list[AttributeCompare] | None,
        search_embedding: np.ndarray | None,
        search_embedding_key: str | None,
        limit: int,
        offset: int,
    ):
        # For performance reasons, do not return vector metadata when top_k>1000
        # A ORM search query automatically returns some fields, so we need to
        # throw an error under these conditions
        if limit > 1000:
            raise ValueError("Pinecone only supports retrieving element values with limit <= 1000")

        if offset > 0:
            raise ValueError("Pinecone doesn't currently support query offsets")

        collection_name = self.transform_collection_name(schema.collection_name())
        primary_key = self._get_primary(schema)
        index = pinecone.Index(index_name=collection_name) 

        # Unlike some other backends, Pinecone requires us to search with some vector as the input
        # We therefore
        missing_vector = search_embedding is None
        if missing_vector:
            info("No vector provided for Pinecone search, using a zero vector to still retrieve content...")
            search_embedding_key, embedding_configuration = self._get_embedding_field(schema)
            search_embedding = np.zeros((embedding_configuration.dim,))

        print("raw", filters)
        filters =  {
            attribute.attr: {"$eq": attribute.value}
            for attribute in filters
        }
        print(filters)
        query_response = index.query(
            filter=filters,
            top_k=limit,
            include_values=False,
            include_metadata=True,
            vector=search_embedding.tolist(),
        )

        objects = []
        for item in query_response.to_dict()["matches"]:
            objects.append(
                QueryResult(
                    result=schema.from_dict(
                        {
                            primary_key: int(item["id"]),
                            **item["metadata"],
                        }
                    ),
                    score=item["score"] if not missing_vector else None,
                )
            )
        return objects

    def flush(self, schema: Type[VectorSchemaBase]):
        # No local caching is involved in Pinecone
        pass

    def load(self, schema: Type[VectorSchemaBase]):
        # No local caching is involved in Pinecone
        pass

    def transform_collection_name(self, collection_name: str):
        return collection_name.replace("_", "-")

    def _assert_valid_collection_name(self, collection_name: str):
        is_valid = all(
            [
                collection_name,
                re_match(r'^[a-z0-9]+(-[a-z0-9]+)*$', collection_name),
                collection_name[0].isalnum(),
                collection_name[-1].isalnum()
            ]
        )

        if not is_valid:
            raise ValueError(f"Invalid collection name: {collection_name}; must be lowercase, alphanumeric, and hyphenated.")

    def _get_embedding_field(self, schema: Type[VectorSchemaBase]):
        embedding_fields = {
            key: value
            for key, value in schema._type_configuration.items()
            if isinstance(value, EmbeddingField)
        }

        if len(embedding_fields) != 1:
            raise ValueError(f"Pinecone only supports one embedding field per collection. {schema} has {len(embedding_fields)} defined: {list(embedding_fields.keys())}.")

        return list(embedding_fields.items())[0]
