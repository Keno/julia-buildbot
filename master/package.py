# Add our packagers on various platforms
julia_packagers  = ["package_osx64"] + ["package_win32", "package_win64"]
julia_packagers += ["package_linux%s"%(arch) for arch in ["32", "64", "armv7l", "ppc64le", "aarch64"]]

# Also add builders for Ubuntu and Centos builders, that won't upload anything at the end
julia_packagers += ["build_ubuntu32", "build_ubuntu64", "build_centos64"]

packager_scheduler = schedulers.AnyBranchScheduler(name="Julia binary packaging", change_filter=util.ChangeFilter(project=['JuliaLang/julia','staticfloat/julia'], branch='master'), builderNames=julia_packagers, treeStableTimer=1)
c['schedulers'].append(packager_scheduler)


# Helper function to generate the necessary julia invocation to get metadata
# about this build such as major/minor versions
@util.renderer
def make_julia_version_command(props):
    command = [
        "usr/bin/julia",
        "-e",
        "println(\"$(VERSION.major).$(VERSION.minor).$(VERSION.patch)\\n$(Base.GIT_VERSION_INFO.commit[1:10])\")"
    ]

    if 'win' in props.getProperty('slavename'):
        command[0] += '.exe'
    return command

# Parse out the full julia version generated by make_julia_version_command's command
def parse_julia_version(return_code, stdout, stderr):
    lines = stdout.split('\n')
    return {
        "majmin": lines[0][:lines[0].rfind('.')],
        "version": lines[0].strip(),
        "shortcommit": lines[1].strip(),
    }

def parse_git_log(return_code, stdout, stderr):
    lines = stdout.split('\n')
    return {
        "commitmessage": lines[0],
        "commitname": lines[1],
        "commitemail": lines[2],
        "authorname": lines[3],
        "authoremail": lines[4],
    }

def parse_artifact_filename(return_code, stdout, stderr):
    if stdout[:26] == 'JULIA_BINARYDIST_FILENAME=':
        artifact_filename = stdout[26:]
        if '-osx' in artifact_filename:
            artifact_filename += '.dmg'
        elif '-winnt' in artifact_filename:
            artifact_filename += '.exe'
        elif '-linux' in artifact_filename:
            artifact_filename += '.tar.gz'
        return {'artifact_filename': artifact_filename}
    return None

@util.renderer
def gen_upload_path(props):
    up_arch = props.getProperty("up_arch")
    majmin = props.getProperty("majmin")
    artifact_filename = props.getProperty("artifact_filename")
    os = get_os_name(props)
    return "julianightlies/bin/%s/%s/%s/%s"%(os, up_arch, majmin, artifact_filename)

@util.renderer
def gen_latest_upload_path(props):
    up_arch = props.getProperty("up_arch")
    artifact_filename = props.getProperty("artifact_filename")
    if artifact_filename[:6] == "julia-":
        artifact_filename = "julia-latest-%s"%(artifact_filename[6:])
    os = get_os_name(props)
    return "julianightlies/bin/latest/%s/%s/%s"%(os, up_arch, artifact_filename)

@util.renderer
def gen_upload_command(props):
    upload_path = gen_upload_path(props)
    artifact_filename = props.getProperty("artifact_filename")
    return ["/bin/bash", "-c", "~/bin/try_thrice ~/bin/aws put --fail --public %s /tmp/julia_package/%s"%(upload_path, artifact_filename)]

@util.renderer
def gen_latest_upload_command(props):
    latest_upload_path = gen_latest_upload_path(props)
    artifact_filename = props.getProperty("artifact_filename")
    return ["/bin/bash", "-c", "~/bin/try_thrice ~/bin/aws put --fail --public %s /tmp/julia_package/%s"%(latest_upload_path, artifact_filename)]

@util.renderer
def gen_download_url(props):
    return 'https://s3.amazonaws.com/'+gen_upload_path(props)


julia_package_env = {
    'CFLAGS':None,
    'CPPFLAGS': None,
    'LLVM_CMAKE': util.Property('llvm_cmake', default=None),
}

# Steps to build a `make binary-dist` tarball that should work on just about every linux ever
julia_package_factory = util.BuildFactory()
julia_package_factory.useProgress = True
julia_package_factory.addSteps([
    # Fetch first (allowing failure if no existing clone is present)
    steps.ShellCommand(
        name="git fetch",
        command=["git", "fetch"],
        flunkOnFailure=False
    ),

    # Clone julia
    steps.Git(
        name="Julia checkout",
        repourl=util.Property('repository', default='git://github.com/JuliaLang/julia.git'),
        mode='incremental',
        method='clean',
        submodules=True,
        clobberOnFailure=True,
        progress=True
    ),

    # Ensure gcc and cmake are installed on OSX
    steps.ShellCommand(
        name="Install necessary brew dependencies",
        command=["brew", "install", "gcc", "cmake"],
        doStepIf=is_osx,
        flunkOnFailure=False
    ),

    # make clean first
    steps.ShellCommand(
        name="make cleanall",
        command=["/bin/bash", "-c", util.Interpolate("make %(prop:flags)s cleanall")],
        env=julia_package_env,
    ),

    # Make, forcing some degree of parallelism to cut down compile times
    # Also build `debug` and `release` in parallel, we should have enough RAM for that now
    steps.ShellCommand(
        name="make",
        command=["/bin/bash", "-c", util.Interpolate("make -j3 %(prop:flags)s debug release")],
        haltOnFailure = True,
        timeout=3600,
        env=julia_package_env,
    ),

    # Test this build
    steps.ShellCommand(
        name="make testall",
        command=["/bin/bash", "-c", util.Interpolate("make %(prop:flags)s testall")],
        haltOnFailure = True,
        timeout=3600,
        env=julia_package_env,
    ),

    # Make win-extras on windows
    steps.ShellCommand(
        name="make win-extras",
        command=["/bin/bash", "-c", util.Interpolate("make %(prop:flags)s win-extras")],
        haltOnFailure = True,
        doStepIf=is_windows,
        env=julia_package_env,
    ),

    # Make binary-dist to package it up
    steps.ShellCommand(
        name="make binary-dist",
        command=["/bin/bash", "-c", util.Interpolate("make %(prop:flags)s binary-dist")],
        haltOnFailure = True,
        timeout=3600,
        env=julia_package_env,
    ),

    # Set a bunch of properties that are useful down the line
    steps.SetPropertyFromCommand(
        name="Get julia version/shortcommit",
        command=make_julia_version_command,
        extract_fn=parse_julia_version,
        want_stderr=False
    ),
    steps.SetPropertyFromCommand(
        name="Get commitmessage",
        command=["git", "log", "-1", "--pretty=format:%s%n%cN%n%cE%n%aN%n%aE"],
        extract_fn=parse_git_log,
        want_stderr=False
    ),
    steps.SetPropertyFromCommand(
        name="Get build artifact filename",
        command=["make", "print-JULIA_BINARYDIST_FILENAME"],
        extract_fn=parse_artifact_filename
    ),

    # Transfer the result to the buildmaster for uploading to AWS
    steps.MasterShellCommand(
        name="mkdir julia_package",
        command=["mkdir", "-p", "/tmp/julia_package"]
    ),

    steps.FileUpload(
        workersrc=util.Interpolate("%(prop:artifact_filename)s"),
        masterdest=util.Interpolate("/tmp/julia_package/%(prop:artifact_filename)s")
    ),

    # Upload it to AWS and cleanup the master!
    steps.MasterShellCommand(
        name="Upload to AWS",
        command=gen_upload_command,
        doStepIf=should_upload,
        haltOnFailure=True
    ),
    steps.MasterShellCommand(
        name="Upload to AWS (latest)",
        command=gen_latest_upload_command,
        doStepIf=should_upload_latest,
        haltOnFailure=True
    ),

    steps.MasterShellCommand(
        name="Cleanup Master",
        command=["rm", "-f", util.Interpolate("/tmp/julia_package/%(prop:artifact_filename)s")],
        doStepIf=should_upload
    ),

    # Trigger a download of this file onto another slave for coverage purposes
    steps.Trigger(schedulerNames=["Julia Coverage Testing"],
        set_properties={
            'url': gen_download_url,
            'commitmessage': util.Property('commitmessage'),
            'commitname': util.Property('commitname'),
            'commitemail': util.Property('commitemail'),
            'authorname': util.Property('authorname'),
            'authoremail': util.Property('authoremail'),
            'shortcommit': util.Property('shortcommit'),
        },
        waitForFinish=False,
        doStepIf=should_run_coverage
    )
])


# Map each builder to each worker
mapping = {
    "package_osx64": "osx10_10-x64",
    "package_win32": "win6_2-x86",
    "package_win64": "win6_2-x64",
    "package_linux32": "centos5_11-x86",
    "package_linux64": "centos5_11-x64",
    "package_linuxarmv7l": "debian7_11-armv7l",
    "package_linuxppc64le": "centos7_2-ppc64le",
    "package_linuxaarch64": "centos7_2-aarch64",

    # These builders don't get uploaded
    "build_ubuntu32": "ubuntu14_04-x86",
    "build_ubuntu64": "ubuntu14_04-x64",
    "build_centos64": "centos7_1-x64",
}
for packager, slave in mapping.iteritems():
    c['builders'].append(util.BuilderConfig(
        name=packager,
        workernames=[slave],
        tags=["Packaging"],
        factory=julia_package_factory
    ))


# Add a scheduler for building release candidates/triggering builds manually
force_build_scheduler = schedulers.ForceScheduler(
    name="force_julia_package",
    label="Force Julia build/packaging",
    builderNames=julia_packagers,
    reason=util.FixedParameter(name="reason", default=""),
    codebases=[
        util.CodebaseParameter(
            "",
            name="",
            branch=util.FixedParameter(name="branch", default=""),
            repository=util.FixedParameter(name="repository", default=""),
            project=util.FixedParameter(name="project", default="Packaging"),
        )
    ],
    properties=[]
)
c['schedulers'].append(force_build_scheduler)
