#!groovy
// Script to create a new beta of Nuxeo Drive.

// Pipeline properties
properties([
    disableConcurrentBuilds(),
    pipelineTriggers([]),
    [$class: 'SchedulerPreference', preferEvenload: true],
    [$class: 'RebuildSettings', autoRebuild: false, rebuildDisabled: false],
])

node('IT') {
    withEnv(["WORKSPACE=${pwd()}"]) {
        stage('Checkout') {
            checkout scm
        }
        stage('Create') {
            sh 'tools/release.sh --create'
        }
        stage('Trigger') {
            def commit_id = sh script: 'git tag -l "release-*" --sort=-taggerdate | head -n1', returnStdout: true
            param.BRANCH_NAME = commit_id
            env.BRANCH_NAME = commit_id

            // Trigger the Drive packages job to build executables and have artifacts.
            // Current job parameters will be forwarded to the triggered job:
            // this way we can choose the commit ID on which to build packages.
            triggerRemoteJob
                parameterFactories: [[
                    $class: 'CurrentBuild',
                    excludesStr: '',
                    includeSensitive: false]
                ],
                mode: [
                    $class: 'TrackProgressAwaitResult',
                    scheduledTimeout: [timeoutStr: '30m'],
                    startedTimeout: [timeoutStr: '30m'],
                    timeout: [timeoutStr: '2h'],
                    whenFailure: [$class: 'StopAsFailure'],
                    whenScheduledTimeout: [$class: 'StopAsFailure'],
                    whenStartedTimeout: [$class: 'StopAsFailure'],
                    whenTimeout: [$class: 'StopAsFailure'],
                    whenUnstable: [$class: 'StopAsFailure']
                ],
                remotePathMissing: [$class: 'StopAsFailure'],
                remotePathUrl: 'jenkins://0ebd1d5127f055c8c674d7778f51ea00/Drive/Drive-packages'
        }
        stage('Publish') {
            dir('dist') {
                deleteDir()
            }
            sh 'tools/release.sh --publish'
        }
    }
}