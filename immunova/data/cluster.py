from immunova.data.fcs import FileGroup
import mongoengine


class Cluster(mongoengine.Document):
    """
    Represents a single cluster generated by a clustering experiment on a single file

    Attributes:
        cluster_id - name associated to cluster
        index - index of cell events associated to cluster
    """
    cluster_id = mongoengine.StringField(required=True)
    index = mongoengine.FileField(db_alias='core', collection_name='cluster_indexes')


class ClusterExperiment(mongoengine.Document):
    """
    Document representation of a cluster experiment as performed on a single sample

    Attributes:
        fcs_file - FileGroup document containing related fcs files
        method - string value of clustering method applied
        root_population - string value indicating the population on which clustering was performed
        clusters - list of generated clusters, represented by Cluster document
    """
    fcs_file = mongoengine.ReferenceField(FileGroup, reverse_delete_rule=4)
    method = mongoengine.StringField(required=True)
    root_population = mongoengine.StringField(required=True, default='root')
    clusters = mongoengine.ListField(mongoengine.ReferenceField(Cluster, reverse_delete_rule=4))

    meta = {
        'db_alias': 'core',
        'collection': 'cluster_experiments'
    }


class MetaFile(mongoengine.EmbeddedDocument):
    """
    Embedded document -> MetaCluster -> ConsensusClusterExperiment
    fcs_file and cluster_id pair to document clusters to associate to a meta-cluster

    Attributes:
        fcs_file - reference to FileGroup to which the cluster belongs
        cluster - reference to Cluster associated to this meta-cluster
    """
    fcs_file = mongoengine.ReferenceField(FileGroup, reverse_delete_rule=4)
    cluster = mongoengine.ReferenceField(Cluster, reverse_delete_rule=4)


class MetaCluster(mongoengine.EmbeddedDocument):
    """
    Embedded document -> ConsensusClusterExperiment
    Meta clustering document collating all clusters associated to given meta cluster

    Attributes:
        cluster_id - cluster id for meta cluster
        contained_clusters - list of embedded MetaFile documents; each provides an fcs_file, cluster pair
    """
    cluster_id = mongoengine.StringField(required=True)
    contained_clusters = mongoengine.EmbeddedDocumentListField(MetaFile)


class ConsensusClusterExperiment(mongoengine.Document):
    """
    Clustering experiment for consensus clustering that yields meta-clusters

    Attributes:
        cluster_experiments - reference list of ClusterExperiment's to perform consensus clustering with
        meta_clusters - list of embedded documents; each describes a meta-cluster generated
    """
    cluster_experiments = mongoengine.ListField(mongoengine.ReferenceField(ClusterExperiment))
    meta_clusters = mongoengine.EmbeddedDocumentListField(MetaCluster)
    meta = {
        'db_alias': 'core',
        'collection': 'consensus_cluster_experiments'
    }