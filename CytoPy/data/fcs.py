from .panel import ChannelMap
from shapely.geometry import Polygon, Point
from shapely.ops import unary_union
from shapely import affinity
from bson.binary import Binary
from warnings import warn
from dask import dataframe as dd
import pandas as pd
import numpy as np
import mongoengine
import pickle
import os


class ClusteringDefinition(mongoengine.Document):
    """
    Defines the methodology and parameters of clustering to apply to an FCS File Group, or in the case of
    meta-clustering, a collection of FCS File Groups from the same FCS Experiment

    Parameters
    ----------
    clustering_uid: str, required
        unique identifier
    method: str, required
        type of clustering performed, either PhenoGraph or FlowSOM
    parameters: list, required
        parameters passed to clustering algorithm (list of tuples)
    features: list, required
        list of channels/markers that clustering is performed on
    transform_method: str, optional, (default:"logicle")
        type of transformation to be applied to data prior to clustering
    root_population: str, required, (default:"root")
        population that clustering is performed on
    cluster_prefix: str, optional, (default: "cluster")
        a prefix to add to the name of each resulting cluster
    meta_method: str, required, (default: False)
        refers to whether the clustering is 'meta-clustering'
    meta_clustering_uid_target: str, optional
        clustering_uid for clustering definition that meta-clustering should target
        in each sample
    """
    clustering_uid = mongoengine.StringField(required=True, unique=True)
    method = mongoengine.StringField(required=True, choices=['PhenoGraph', 'FlowSOM', 'ConsensusClustering'])
    parameters = mongoengine.ListField(required=True)
    features = mongoengine.ListField(required=True)
    transform_method = mongoengine.StringField(required=False, default='logicle')
    root_population = mongoengine.StringField(required=True, default='root')
    cluster_prefix = mongoengine.StringField(required=False, default='cluster')
    meta_method = mongoengine.BooleanField(required=True, default=False)
    meta_clustering_uid_target = mongoengine.StringField(required=False)

    meta = {
        'db_alias': 'core',
        'collection': 'cluster_definitions'
    }


class Cluster(mongoengine.EmbeddedDocument):
    """
    Represents a single cluster generated by a clustering experiment on a single file

    Parameters
    ----------
    cluster_id: str, required
        name associated to cluster
    index: FileField
        index of cell events associated to cluster (very large array)
    n_events: int, required
        number of events in cluster
    prop_of_root: float, required
        proportion of events in cluster relative to root population
    cluster_experiment: RefField
        reference to ClusteringDefinition
    meta_cluster_id: str, optional
        associated meta-cluster
    """
    cluster_id = mongoengine.StringField(required=True)
    index = mongoengine.FileField(db_alias='core', collection_name='cluster_indexes')
    n_events = mongoengine.IntField(required=True)
    prop_of_root = mongoengine.FloatField(required=True)
    cluster_experiment = mongoengine.ReferenceField(ClusteringDefinition)
    meta_cluster_id = mongoengine.StringField(required=False)

    def save_index(self, data: np.array) -> None:
        """
        Save the index of data that corresponds to cells belonging to this cluster

        Parameters
        ----------
        data: np.array, required
            Numpy array of single cell events data

        Returns
        -------
        None
        """
        if self.index:
            self.index.replace(Binary(pickle.dumps(data, protocol=2)))
        else:
            self.index.new_file()
            self.index.write(Binary(pickle.dumps(data, protocol=2)))
            self.index.close()

    def load_index(self) -> np.array:
        """
        Load the index of data that corresponds to cells belonging to this cluster

        Returns
        -------
        np.array
            Array of single cell events data
        """
        return pickle.loads(bytes(self.index.read()))


class ControlIndex(mongoengine.EmbeddedDocument):
    """
    Cached index for population in an associated control

    Parameters
    ----------
    control_id: str
        Name of the control file
    index: FileField
        numpy array storing index of events that belong to population
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.index = kwargs.get("index", None)

    control_id = mongoengine.StringField()
    _index_file = mongoengine.FileField(db_alias='core', collection_name='control_indexes')

    def save_index(self, data: np.array) -> None:
        """
        Given a new numpy array of index values, serialise and commit data to database

        Parameters
        ----------
        data: np.array
            Array of index values

        Returns
        -------
        None
        """
        if self.index is not None:
            if self._index_file:
                self._index_file.replace(Binary(pickle.dumps(data, protocol=2)))
            else:
                self._index_file.new_file()
                self._index_file.write(Binary(pickle.dumps(data, protocol=2)))
                self._index_file.close()

    def load_index(self) -> np.array:
        """
        Retrieve the index values for the given population

        Returns
        -------
        np.array
            Array of index values
        """
        data = self._index_file.read()
        if data:
            self.index = pickle.loads(bytes(data))
            return self.index
        return None


class PopulationGeometry(mongoengine.EmbeddedDocument):
    """
    Geometric shape generated by non-threshold generating Gate
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._shape = None

    x_values = mongoengine.ListField()
    y_values = mongoengine.ListField()
    width = mongoengine.FloatField()
    height = mongoengine.FloatField()
    center = mongoengine.ListField()
    angle = mongoengine.FloatField()
    x_threshold = mongoengine.FloatField()
    y_threshold = mongoengine.FloatField()

    @property
    def shape(self):
        """
        Generates a Shapely Polygon object.

        Returns
        -------
        Shapely.geometry.Polygon
        """
        if self.x_values and self.y_values:
            return Polygon([(x, y) for x, y in zip(self.x_values, self.y_values)])
        elif all([self.width,
                  self.height,
                  self.center,
                  self.angle]):
            circle = Point(self.center).buffer(1)
            ellipse = affinity.rotate(affinity.scale(circle, self.width, self.height),
                                      self.angle)
            return ellipse
        return None

    def overlap(self,
                comparison_poly: Polygon,
                threshold: float = 0.):
        if self.shape is None:
            warn("PopulationGeometry properties are incomplete. Cannot determine shape.")
            return None
        if self.shape.intersects(comparison_poly):
            overlap = float(self.shape.intersection(comparison_poly).area / self.shape.area)
            if overlap >= threshold:
                return overlap
        return 0.


class Population(mongoengine.EmbeddedDocument):
    """
    Cached populations; stores the index of events associated to a population for quick loading.

    Parameters
    ----------
    population_name: str, required
        name of population
    index: FileField
        numpy array storing index of events that belong to population
    prop_of_parent: float, required
        proportion of events as a percentage of parent population
    prop_of_total: float, required
        proportion of events as a percentage of all events
    warnings: list, optional
        list of warnings associated to population
    parent: str, required, (default: "root")
        name of parent population
    children: list, optional
        list of child populations (list of strings)
    geom: list, required
        list of key value pairs (tuples; (key, value)) for defining geom of population e.g.
        the definition for an ellipse that 'captures' the population
    clusters: EmbeddedDocListField
        list of associated Cluster documents
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.index = kwargs.get("index", None)

    population_name = mongoengine.StringField()
    _index_file = mongoengine.FileField(db_alias='core', collection_name='population_indexes')
    _n = mongoengine.IntField()
    parent = mongoengine.StringField(required=True, default='root')
    prop_of_parent = mongoengine.FloatField()
    prop_of_total = mongoengine.FloatField()
    warnings = mongoengine.ListField()
    geom = mongoengine.EmbeddedDocument(PopulationGeometry)
    definition = mongoengine.StringField()
    clusters = mongoengine.EmbeddedDocumentListField(Cluster)
    control_idx = mongoengine.EmbeddedDocumentListField(ControlIndex)

    @property
    def n(self):
        if self.index is None:
            return 0
        return len(self.index)

    def get_ctrl(self,
                 ctrl_id: str) -> ControlIndex or None:
        """
        Returns
        -------
        bool or ControlIndex
        """
        for c in self.control_idx:
            if c.control_id == ctrl_id:
                return c
        return None

    def save_index(self) -> None:
        """
        Given a new numpy array of index values, serialise and commit data to database
        """
        assert self.index is not None, "Index is null"
        if self._index_file:
            self._index_file.replace(Binary(pickle.dumps(self.index, protocol=2)))
        else:
            self._index_file.new_file()
            self._index_file.write(Binary(pickle.dumps(self.index, protocol=2)))
            self._index_file.close()

    def load_index(self) -> np.array:
        """
        Retrieve the index values for the given population

        Returns
        -------
        np.array
            Array of index values
        """
        data = self._index_file.read()
        if data:
            self.index = pickle.loads(bytes(data))
            return self.index
        return None

    def list_clustering_experiments(self) -> set:
        """
        Generate a list of clustering experiment UIDs

        Returns
        -------
        set
            Clustering experiment UIDs
        """
        return set([c.cluster_experiment.clustering_uid for c in self.clusters])

    def get_many_clusters(self, clustering_uid: str) -> list:
        """
        Given a clustering UID return the associated clusters

        Parameters
        ----------
        clustering_uid: str
             UID for clusters of interest

        Returns
        -------
        list
            List of cluster documents
        """
        if clustering_uid not in self.list_clustering_experiments():
            raise ValueError(f'Error: a clustering experiment with UID {clustering_uid} does not exist')
        return [c for c in self.clusters if c.cluster_experiment.clustering_uid == clustering_uid]

    def delete_clusters(self, clustering_uid: str or None = None, drop_all: bool = False) -> None:
        """
        Given a clustering UID, remove associated clusters

        Parameters
        ----------
        clustering_uid: str
            UID for clusters to be removed
        drop_all: bool
            If True, all clusters are removed regardless of clustering experiment UID

        Returns
        -------
        None
        """
        if drop_all:
            self.clusters = []
            return
        assert clustering_uid, 'Must provide a valid clustering experiment UID'
        if clustering_uid not in self.list_clustering_experiments():
            raise ValueError(f'Error: a clustering experiment with UID {clustering_uid} does not exist')
        self.clusters = [c for c in self.clusters if c.cluster_experiment.clustering_uid != clustering_uid]

    def replace_cluster_experiment(self, current_uid: str, new_cluster_definition: ClusteringDefinition) -> None:
        """
        Given a clustering UID and new clustering definition, replace the clustering definition
        for all associated clusters

        Parameters
        ----------
        current_uid: str
            UID of clusters to be updated
        new_cluster_definition: ClusteringDefinition
            New clustering definition

        Returns
        -------
        None
        """
        for c in self.clusters:
            try:
                if c.cluster_experiment.clustering_uid == current_uid:
                    c.cluster_experiment = new_cluster_definition
            except mongoengine.errors.DoesNotExist:
                c.cluster_experiment = new_cluster_definition

    def update_cluster(self, cluster_id: str, new_cluster: Cluster) -> None:
        """
        Given the ID for a specific cluster, replace the cluster with a new Cluster document

        Parameters
        ----------
        cluster_id: str
            Cluster ID for cluster to replace
        new_cluster: Cluster
            Cluster document to use for updating cluster

        Returns
        -------
        None
        """
        self.clusters = [c for c in self.clusters if c.cluster_id != cluster_id]
        self.clusters.append(new_cluster)

    def list_clusters(self, meta: bool = True) -> set:
        """
        Returns a set of all existing clusters.

        Parameters
        ----------
        meta: bool
            If True, search is isolated to meta-clusters

        Returns
        -------
        set
            Cluster IDs
        """
        if meta:
            return set([c.meta_cluster_id for c in self.clusters])
        return set([c.cluster_id for c in self.clusters])

    def get_cluster(self, cluster_id: str, meta: bool = True) -> (Cluster, np.array):
        """
        Given a cluster ID return the Cluster document and array of index values

        Parameters
        ----------
        cluster_id: str
            ID for cluster to pull from database
        meta: bool
            If True, search will be isolated to clusters associated to a meta cluster ID

        Returns
        -------
        Cluster, np.array
            Cluster Document, Array of index values
        """

        if meta:
            clusters = [c for c in self.clusters if c.meta_cluster_id == cluster_id]
            assert clusters, f'No such cluster(s) with meta clustering ID {cluster_id}'
            idx = [c.load_index() for c in clusters]
            idx = np.unique(np.concatenate(idx, axis=0), axis=0)
            return clusters, idx
        clusters = [c for c in self.clusters if c.cluster_id == cluster_id]
        assert clusters, f'No such cluster with clustering ID {cluster_id}'
        assert len(clusters) == 1, f'Multiple clusters with ID {cluster_id}'
        return clusters[0], clusters[0].load_index()


class File(mongoengine.EmbeddedDocument):
    """
    Document representation of a single FCS file.

    Parameters
    -----------
    file_id: str, required
        Unique identifier for fcs file
    file_type: str, required, (default='complete')
        One of either 'complete' or 'control'; signifies the type of data stored
    data: FileField
        Numpy array of fcs events data
    compensated: bool, required, (default=False)
        Boolean value, if True then data have been compensated
    channel_mappings: list
        List of standarised channel/marker mappings (corresponds to column names of underlying data)
    """
    file_id = mongoengine.StringField(required=True)
    file_type = mongoengine.StringField(default='complete')
    data = mongoengine.FileField(db_alias='core', collection_name='fcs_file_data')
    compensated = mongoengine.BooleanField(default=False)
    channel_mappings = mongoengine.EmbeddedDocumentListField(ChannelMap)

    def get(self,
            sample: int or None = None,
            as_type: str = 'dataframe') -> np.array:
        """
        Retrieve single cell data from database

        Parameters
        ----------
        sample: int, optional
            If an integer value is given, a random sample of this size is returned

        Returns
        -------
        Numpy.array
            Array of single cell data
        """

        assert as_type in ['dataframe', 'array'], "as_type should be 'array' or 'dataframe'"
        self.data.seek(0)
        if as_type == 'dataframe':
            df = pd.read_pickle(self.data.read())
            if sample and sample < df.shape[0]:
                return df.sample(sample)
            return df
        else:
            data = np.load(self.data.read(), allow_pickle=True)
            if sample and sample < data.shape[0]:
                idx = np.random.randint(0, data.shape[0], size=sample)
                return data[idx, :]
        return data

    def put(self, data: np.array) -> None:
        """
        Save single cell data to database

        Parameters
        ----------
        data: Numpy.array
            Single cell data (as a numpy array) to save to database

        Returns
        -------
        None
        """
        if self.data:
            self.data.replace(Binary(pickle.dumps(data, protocol=2)))
        else:
            self.data.new_file()
            self.data.write(Binary(pickle.dumps(data, protocol=2)))
            self.data.close()


class FileGroup(mongoengine.Document):
    """
    Document representation of a file group; a selection of related fcs files (e.g. a sample and it's associated
    controls)

    Parameters
    ----------
    primary_id: str, required
        Unique ID to associate to group
    files: EmbeddedDocList
        List of File objects
    flags: str, optional
        Warnings associated to file group
    notes: str, optional
        Additional free text
    populations: EmbeddedDocList
        Populations derived from this file group
    gates: EmbeddedDocList
        Gate objects that have been applied to this file group
    collection_datetime: DateTime, optional
        Date and time of sample collection
    processing_datetime: DateTime, optional
        Date and time of sample processing
    """
    primary_id = mongoengine.StringField(required=True)
    files = mongoengine.EmbeddedDocumentListField(File)
    flags = mongoengine.StringField(required=False)
    notes = mongoengine.StringField(required=False),
    collection_datetime = mongoengine.DateTimeField(required=False)
    processing_datetime = mongoengine.DateTimeField(required=False)
    populations = mongoengine.EmbeddedDocumentListField(Population)
    meta = {
        'db_alias': 'core',
        'collection': 'fcs_files'
    }

    def save(self, *args, **kwargs):
        root_n = [p for p in self.populations if p.population_name == "root"][0].n
        for p in self.populations:
            parent_n = [p for p in self.populations if p.population_name == p.parent][0].n
            p.prop_of_parent = p.n/parent_n
            p.prop_of_total = p.n/root_n
            for ctrl in p.control_idx:
                ctrl.save_index()
            p.save_index()
        super().save(*args, **kwargs)

    def list_controls(self) -> list:
        """
        Return a list of file IDs for associated control files

        Returns
        -------
        list
        """
        return [f.file_id.replace(f'{self.primary_id}_', '') for f in self.files if f.file_type == 'control']

    def list_gated_controls(self) -> list:
        """
        List ID of controls that have a cached index in each population of the saved population tree
        (i.e. they have been gated)

        Returns
        -------
        list
            List of control IDs for gated controls
        """
        ctrls = self.list_controls()
        return [c for c in ctrls if all([p.get_ctrl(c) is not None for p in self.populations])]

    def list_populations(self) -> iter:
        """
        Yields list of population names
        Returns
        -------
        Generator
        """
        for p in self.populations:
            yield p.population_name

    def delete_clusters(self, clustering_uid: str or None = None, drop_all: bool = False):
        """
        Delete all cluster attaining to a given clustering UID

        Parameters
        ----------
        clustering_uid: str
            Unique identifier for clustering experiment that should have clusters deleted from file
        drop_all: bool
            If True, all clusters for every population are dropped from database regardless of the
            clustering experiment they are associated too
        Returns
        -------
        None
        """
        if not drop_all:
            assert clustering_uid, 'Must provide a valid clustering experiment UID'
        for p in self.populations:
            p.delete_clusters(clustering_uid, drop_all)
        self.save()

    def delete_populations(self, populations: list or str) -> None:
        """
        Delete given populations

        Parameters
        ----------
        populations: list or str
            Either a list of populations (list of strings) to remove or a single population as a string

        Returns
        -------
        None
        """
        if populations == all:
            self.populations = []
        else:
            self.populations = [p for p in self.populations if p.population_name not in populations]
        self.save()

    def update_population(self, population_name: str, new_population: Population):
        """
        Given an existing population name, replace that population with the new population document

        Parameters
        -----------
        population_name: str
            Name of population to be replaced
        new_population: Population
            Updated/new Population document

        Returns
        --------
        None
        """
        self.populations = [p for p in self.populations if p.population_name != population_name]
        self.populations.append(new_population)
        self.save()

    def validity(self) -> bool:
        """
        Returns True if FileGroup is deemed 'valid'; that is, the term 'invalid' is absent from the
        'flags' attribute.

        Returns
        -------
        bool
            True if valid, else False
        """
        if self.flags is None:
            return True
        if 'invalid' in self.flags:
            return False
        return True

    def get_population(self, population_name: str) -> Population:
        """
        Given the name of a population associated to the FileGroup, returns the Population object, with
        index and control index ready loaded.

        Parameters
        ----------
        population_name: str
            Name of population to retrieve from database

        Returns
        -------
        Population
        """
        p = [p for p in self.populations if p.population_name == population_name]
        assert p, f'Population {population_name} does not exist'
        assert len(p) == 1, f"Multiple populations with name {population_name}, this should never happen!"
        p[0].load_index()
        for ctrl in p[0].control_idx:
            ctrl.load_index()
        return p[0]

    def get_population_by_parent(self, parent: str):
        """
        Given the name of some parent population, return a list of Population object whom's parent matches

        Parameters
        ----------
        parent: str
            Name of the parent population to search for

        Returns
        -------
        List
            List of Populations
        """
        return [p for p in self.populations if p.parent == parent]


def merge_populations(left_p: Population,
                      right_p: Population):
    assert left_p.parent == right_p.parent, "Parent populations do not match"
    # check that geometries overlap
    assert left_p.geom.shape.intersects(right_p.geom.shape), "Invalid: cannot merge non-overlapping populations"
    if len(left_p.clusters) > 0 or len(right_p.clusters) > 0:
        warn("Associated clusters are now void. Repeat clustering on new population")
    if len(left_p.control_idx) > 0 or len(right_p.control_idx) > 0:
        warn("Associated control indexes are now void. Repeat control gating on new population")
    new_definition = None
    if left_p.definition and right_p.definition:
        new_definition = ",".join([left_p.definition, right_p.definition])
    new_population = Population(population_name=left_p.population_name,
                                n=len(left_p.index) + len(right_p.index),
                                parent=left_p.parent,
                                warnings=left_p.warnings+right_p.warnings+["MERGED POPULATION"],
                                index=np.unique(np.concatenate(left_p.index, right_p.index)),
                                geom=unary_union([p.geom.shape for p in [left_p, right_p]]),
                                definition=new_definition)
    return new_population
