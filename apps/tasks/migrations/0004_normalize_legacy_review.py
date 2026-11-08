from django.db import migrations


def normalize_legacy_review(apps, schema_editor):
    Task = apps.get_model("tasks", "Task")
    legacy = Task.objects.using(schema_editor.connection.alias).filter(status="review")
    legacy.filter(assignee_id__isnull=False).update(status="in_progress")
    legacy.filter(assignee_id__isnull=True).update(status="pending")


class Migration(migrations.Migration):
    dependencies = [("tasks", "0003_task_workflows")]

    operations = [
        migrations.RunPython(normalize_legacy_review, reverse_code=migrations.RunPython.noop),
    ]
