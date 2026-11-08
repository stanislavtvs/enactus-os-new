PLATFORM_ADMIN_GROUP = "enactus-platform-admin"


def is_platform_admin(user):
    return user.is_authenticated and user.is_active and user.groups.filter(name=PLATFORM_ADMIN_GROUP).exists()
