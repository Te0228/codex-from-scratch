"""JSON in / JSON out task-list program. Deliberate status-filter defect."""
import json
import sys


def filter_tasks(tasks, status=None):
    if status is None:
        return tasks
    return [task for task in tasks if task.get("state") == status]


if __name__ == "__main__":
    request = json.load(sys.stdin)
    print(json.dumps(filter_tasks(request["tasks"], request.get("status"))))
