import os
from .compute_node import ComputeNode


class ThetaKnlNode(ComputeNode):

    cpu_ids = list(range(64))
    gpu_ids = []

    @classmethod
    def get_job_nodelist(cls):
        """
        Get all compute nodes allocated in the current job context
        """
        node_str = os.environ["COBALT_PARTNAME"]
        # string like: 1001-1005,1030,1034-1200
        node_ids = []
        ranges = node_str.split(",")
        for node_range in ranges:
            lo, *hi = node_range.split("-")
            lo = int(lo)
            if hi:
                hi = int(hi[0])
                node_ids.extend(list(range(lo, hi + 1)))
            else:
                node_ids.append(lo)

        return [cls(node_id, f"nid{node_id:05d}") for node_id in node_ids]

    @staticmethod
    def get_batch_job_id():
        id = os.environ.get("COBALT_JOBID")
        if id is not None:
            return int(id)
        return None