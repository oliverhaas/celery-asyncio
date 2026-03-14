"""URL routes that demonstrate enqueueing Django tasks and fetching results."""

from django.http import HttpResponse, JsonResponse
from django.urls import path

from . import tasks


def index(request):
    return HttpResponse(
        "<h2>Django Tasks + Celery Example</h2>"
        "<ul>"
        '<li><a href="/enqueue/add/?x=2&y=3">Enqueue add(2, 3)</a></li>'
        '<li><a href="/enqueue/slow_add/?x=10&y=20">Enqueue slow_add(10, 20)</a></li>'
        '<li><a href="/enqueue/urgent/?message=hello">Enqueue high-priority task</a></li>'
        '<li><a href="/enqueue/context/?data=test">Enqueue task with context</a></li>'
        "<li>/result/&lt;task_id&gt;/ — check a task result</li>"
        "</ul>",
        content_type="text/html",
    )


def enqueue_add(request):
    x = int(request.GET.get("x", 1))
    y = int(request.GET.get("y", 2))
    result = tasks.add.enqueue(x, y)
    return JsonResponse({"task_id": result.id, "status": str(result.status)})


def enqueue_slow_add(request):
    x = int(request.GET.get("x", 1))
    y = int(request.GET.get("y", 2))
    result = tasks.slow_add.enqueue(x, y)
    return JsonResponse({"task_id": result.id, "status": str(result.status)})


def enqueue_urgent(request):
    message = request.GET.get("message", "default")
    result = tasks.high_priority_task.enqueue(message)
    return JsonResponse({"task_id": result.id, "status": str(result.status)})


def enqueue_context(request):
    data = request.GET.get("data", "hello")
    result = tasks.task_with_context.enqueue(data)
    return JsonResponse({"task_id": result.id, "status": str(result.status)})


def get_result(request, task_id):
    result = tasks.add.get_result(task_id)
    response = {
        "task_id": result.id,
        "status": str(result.status),
        "finished_at": str(result.finished_at) if result.finished_at else None,
    }
    if result.is_finished:
        if result.status.value == "SUCCESSFUL":
            response["return_value"] = result.return_value
        else:
            response["errors"] = [
                {"exception": e.exception_class_path, "traceback": e.traceback} for e in result.errors
            ]
    return JsonResponse(response)


urlpatterns = [
    path("", index),
    path("enqueue/add/", enqueue_add),
    path("enqueue/slow_add/", enqueue_slow_add),
    path("enqueue/urgent/", enqueue_urgent),
    path("enqueue/context/", enqueue_context),
    path("result/<str:task_id>/", get_result),
]
