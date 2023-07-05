# Copyright (C) 2023 CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

from datetime import timedelta
from django.utils import timezone

import django_rq
from django.conf import settings

from uuid import uuid4

from cvat.apps.engine.models import Job, Task, Project

from cvat.apps.analytics_report.models import AnalyticsReport
from cvat.apps.analytics_report.report.primary_metrics import (
    JobAnnotationSpeed,
    JobAnnotationTime,
    JobObjects,
)
from cvat.apps.analytics_report.report.derived_metrics import (
    JobTotalAnnotationSpeed,
    JobTotalObjectCount,

    TaskAnnotationSpeed,
    TaskObjects,
    TaskAnnotationTime,
    TaskTotalAnnotationSpeed,
    TaskTotalObjectCount,

    ProjectAnnotationSpeed,
    ProjectObjects,
    ProjectAnnotationTime,
    ProjectTotalAnnotationSpeed,
    ProjectTotalObjectCount,
)

class JobAnalyticsReportUpdateManager():
    _QUEUE_JOB_PREFIX_TASK = "update-analytics-report-task-"
    _QUEUE_JOB_PREFIX_PROJECT = "update-analytics-report-project-"
    _RQ_CUSTOM_ANALYTICS_CHECK_JOB_TYPE = "custom_analytics_check"
    _JOB_RESULT_TTL = 120

    @classmethod
    def _get_analytics_check_job_delay(cls) -> timedelta:
        return timedelta(seconds=settings.ANALYTICS_CHECK_JOB_DELAY)

    def _get_scheduler(self):
        return django_rq.get_scheduler(settings.CVAT_QUEUES.ANALYTICS_REPORTS.value)

    def _get_queue(self):
        return django_rq.get_queue(settings.CVAT_QUEUES.ANALYTICS_REPORTS.value)

    def _make_queue_job_prefix(self, obj) -> str:
        if isinstance(obj, Task):
            return f"{self._QUEUE_JOB_PREFIX_TASK}{obj.id}-"
        else:
            return f"{self._QUEUE_JOB_PREFIX_PROJECT}{obj.id}-"

    def _make_custom_analytics_check_job_id(self) -> str:
        return uuid4().hex

    def _make_initial_queue_job_id(self, obj) -> str:
        return f"{self._make_queue_job_prefix(obj)}initial"

    def _make_regular_queue_job_id(self, obj, start_time: timezone.datetime) -> str:
        return f"{self._make_queue_job_prefix(obj)}{start_time.timestamp()}"

    @classmethod
    def _get_last_report_time(cls, obj) :
        report = obj.analytics_report
        if report:
            return report.created_date
        return None

    def _find_next_job_id(
        self, existing_job_ids, obj, *, now
    ) -> str:
        job_id_prefix = self._make_queue_job_prefix(obj)

        def _get_timestamp(job_id: str) -> timezone.datetime:
            job_timestamp = job_id.split(job_id_prefix, maxsplit=1)[-1]
            if job_timestamp == "initial":
                return timezone.datetime.min.replace(tzinfo=timezone.utc)
            else:
                return timezone.datetime.fromtimestamp(float(job_timestamp), tz=timezone.utc)

        max_job_id = max(
            (j for j in existing_job_ids if j.startswith(job_id_prefix)),
            key=_get_timestamp,
            default=None,
        )
        max_timestamp = _get_timestamp(max_job_id) if max_job_id else None

        last_update_time = self._get_last_report_time(obj)
        if last_update_time is None:
            # Report has never been computed, is queued, or is being computed
            queue_job_id = self._make_initial_queue_job_id(obj)
        elif max_timestamp is not None and now < max_timestamp:
            # Reuse the existing next job
            queue_job_id = max_job_id
        else:
            # Add an updating job in the queue in the next time frame
            delay = self._get_analytics_check_job_delay()
            intervals = max(1, 1 + (now - last_update_time) // delay)
            next_update_time = last_update_time + delay * intervals
            queue_job_id = self._make_regular_queue_job_id(obj, next_update_time)

        return queue_job_id

    class AnalyticsReportsNotAvailable(Exception):
        pass

    def schedule_analytics_report_autoupdate_job(self, *, job=None, task=None, project=None):
        now = timezone.now()
        delay = self._get_analytics_check_job_delay()
        next_job_time = now.utcnow() + delay

        scheduler = self._get_scheduler()
        existing_job_ids = set(j.id for j in scheduler.get_jobs(until=next_job_time))

        target_obj = None
        cvat_project_id = None
        cvat_task_id = None
        if job is not None:
            if job.segment.task.project:
                target_obj = job.segment.task.project
                cvat_project_id = target_obj.id
            else:
                target_obj = job.segment.task
                cvat_task_id = target_obj.id
        elif task is not None:
            if task.project:
                target_obj = task.project
                cvat_project_id = target_obj.id
            else:
                target_obj = task
                cvat_task_id = target_obj.id
        elif project is not None:
            target_obj = project
            cvat_project_id = project.id

        queue_job_id = self._find_next_job_id(existing_job_ids, target_obj, now=now)
        if queue_job_id not in existing_job_ids:
            scheduler.enqueue_at(
                next_job_time,
                self._check_job_analytics,
                cvat_task_id=cvat_task_id,
                cvat_project_id=cvat_project_id,
                job_id=queue_job_id,
            )

    def schedule_analytics_check_job(self, *, job=None, task=None, project=None, user_id):
        rq_id = self._make_custom_analytics_check_job_id()

        queue = self._get_queue()
        queue.enqueue(
            self._check_job_analytics,
            cvat_job_id=job.id if job is not None else None,
            cvat_task_id=task.id if task is not None else None,
            cvat_project_id=project.id if project is not None else None,
            job_id=rq_id,
            meta={"user_id": user_id, "job_type": self._RQ_CUSTOM_ANALYTICS_CHECK_JOB_TYPE},
            result_ttl=self._JOB_RESULT_TTL,
            failure_ttl=self._JOB_RESULT_TTL,
        )

        return rq_id

    def get_analytics_check_job(self, rq_id: str):
        queue = self._get_queue()
        rq_job = queue.fetch_job(rq_id)

        if rq_job and not self.is_custom_analytics_check_job(rq_job):
            rq_job = None

        return rq_job

    def is_custom_analytics_check_job(self, rq_job) -> bool:
        return rq_job.meta.get("job_type") == self._RQ_CUSTOM_ANALYTICS_CHECK_JOB_TYPE

    @classmethod
    def _check_job_analytics(cls, *, cvat_job_id: int=None, cvat_task_id: int=None, cvat_project_id: int=None) -> int:
        if cvat_job_id is not None:
            db_job = Job.objects.select_related("analytics_report").get(pk=cvat_job_id)
            return cls()._compute_report_for_job(db_job)
        elif cvat_task_id is not None:
            db_task = Task.objects.select_related("analytics_report").prefetch_related("segment_set__job_set").get(pk=cvat_task_id)
            return cls()._compute_report_for_task(db_task)
        elif cvat_project_id is not None:
            db_project = Project.objects.select_related("analytics_report").prefetch_related("tasks__segment_set__job_set").get(pk=cvat_project_id)
            return cls()._compute_report_for_project(db_project)

    @staticmethod
    def _get_statistics_entry(statistics_object):
        return {
            "title": statistics_object.title,
            "description": statistics_object.description,
            "granularity": statistics_object.granularity,
            "default_view": statistics_object.default_view,
            "transformations": statistics_object.transformations,
            "dataseries": statistics_object.calculate(),
        }

    def _compute_report_for_job(self, db_job):
        db_report = getattr(db_job, "analytics_report", None)
        was_created = False
        if db_report is None:
            db_report = AnalyticsReport.objects.create(
                job_id=db_job.id,
                statistics={})
            db_report.save()
            was_created = True
            db_job.analytics_report = db_report

        # recalculate the report if it is not relevant
        if db_report.created_date < db_job.updated_date or was_created:

            annotation_speed = JobAnnotationSpeed(db_job)
            objects = JobObjects(db_job)
            annotation_time = JobAnnotationTime(db_job)

            statistics = {
                "objects": self._get_statistics_entry(objects),
                "annotation_speed": self._get_statistics_entry(annotation_speed),
                "annotation_time": self._get_statistics_entry(annotation_time),
            }

            total_annotation_speed = JobTotalAnnotationSpeed(db_job, primary_statistics=statistics["annotation_speed"])
            total_object_count = JobTotalObjectCount(db_job, primary_statistics=statistics["annotation_speed"])

            statistics["total_annotation_speed"] = self._get_statistics_entry(total_annotation_speed)
            statistics["total_object_count"] = self._get_statistics_entry(total_object_count)

            db_report.statistics = statistics
            db_report.save()

        return db_report

    def _compute_report_for_task(self, db_task):
        db_report = getattr(db_task, "analytics_report", None)
        was_created = False
        if db_report is None:
            db_report = AnalyticsReport.objects.create(task_id=db_task.id, statistics={})
            db_report.save()
            db_task.analytics_report = db_report
            was_created = True

        # recalculate the report if it is not relevant
        if db_report.created_date < db_task.updated_date or was_created:
            job_reports = []
            for db_segment in db_task.segment_set.all():
                for db_job in db_segment.job_set.all():
                    job_reports.append(self._compute_report_for_job(db_job))

            objects = TaskObjects(db_task, [jr.statistics["objects"] for jr in job_reports])
            annotation_speed = TaskAnnotationSpeed(db_task, [jr.statistics["annotation_speed"] for jr in job_reports])
            annotation_time = TaskAnnotationTime(db_task, [jr.statistics["annotation_time"] for jr in job_reports])
            total_annotation_speed = TaskTotalAnnotationSpeed(db_task, [jr.statistics["annotation_speed"] for jr in job_reports])
            total_object_count = TaskTotalObjectCount(db_task, [jr.statistics["annotation_speed"] for jr in job_reports])

            statistics = {
                "objects": self._get_statistics_entry(objects),
                "annotation_speed": self._get_statistics_entry(annotation_speed),
                "annotation_time": self._get_statistics_entry(annotation_time),
                "total_annotation_speed": self._get_statistics_entry(total_annotation_speed),
                "total_object_count": self._get_statistics_entry(total_object_count),
            }

            db_report.statistics = statistics
            db_report.save()

        return db_report

    def _compute_report_for_project(self, db_project):
        db_report = getattr(db_project, "analytics_report", None)
        was_created = False
        if db_report is None:
            db_report = AnalyticsReport.objects.create(project_id=db_project.id, statistics={})
            db_report.save()
            db_project.analytics_report = db_report
            was_created = True

        # recalculate the report if it is not relevant
        if db_report.created_date < db_project.updated_date or was_created:
            job_reports = []
            for db_task in db_project.tasks.all():
                self._compute_report_for_task(db_task)
                for db_segment in db_task.segment_set.all():
                    for db_job in db_segment.job_set.all():
                        db_job.analytics_report.refresh_from_db()
                        job_reports.append(self._compute_report_for_job(db_job))

            objects = ProjectObjects(db_project, [jr.statistics["objects"] for jr in job_reports])
            annotation_speed = ProjectAnnotationSpeed(db_task, [jr.statistics["annotation_speed"] for jr in job_reports])
            annotation_time = ProjectAnnotationTime(db_task, [jr.statistics["annotation_time"] for jr in job_reports])
            total_annotation_speed = ProjectTotalAnnotationSpeed(db_task, [jr.statistics["annotation_speed"] for jr in job_reports])
            total_object_count = ProjectTotalObjectCount(db_task, [jr.statistics["annotation_speed"] for jr in job_reports])

            statistics = {
                "objects": self._get_statistics_entry(objects),
                "annotation_speed": self._get_statistics_entry(annotation_speed),
                "annotation_time": self._get_statistics_entry(annotation_time),
                "total_annotation_speed": self._get_statistics_entry(total_annotation_speed),
                "total_object_count": self._get_statistics_entry(total_object_count),
            }

            db_report.statistics = statistics
            db_report.save()

        return db_report

    def _get_current_job(self):
        from rq import get_current_job

        return get_current_job()