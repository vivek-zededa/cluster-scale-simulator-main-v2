pipeline {
  agent {
    label 'zedcloud'
  }

  options {
    buildDiscarder(logRotator(numToKeepStr: '20'))
    checkoutToSubdirectory("CHANGEME")
    disableConcurrentBuilds()

    timeout (time: 75, unit: 'MINUTES')

    timestamps()
  }

  environment {
    SRC="${WORKSPACE}/CHANGEME"
    ARTIFACTDIR="${WORKSPACE}/tmp"
  }

  stages {
    stage('Checkout jenkins-library') {
      steps {
        library identifier: "zededa@main",
                retriever: modernSCM(
                          [$class: 'GitSCMSource',
                           remote: 'https://github.com/zededa/jenkins-library.git',
                           credentialsId: 'github-build-at-zededa.net'])
      }
    }

    stage('Apache Yetus') {
      steps {
        script {
          yetusSimpleTest(srcDir: "${SRC}", yetusPatchDir: "yetus", nobuild: true)
        }
      }
    }

    stage('Build and Push Docker Image') {
      steps {
        withCredentials([usernamePassword(credentialsId: 'docker-hub-cred',
                                          usernameVariable: 'DOCKER_USER',
                                          passwordVariable: 'DOCKER_PASS')]) {
          sh """
            cd ${SRC}
            docker login -u \${DOCKER_USER} -p \${DOCKER_PASS}
            make docker-build
            make docker-push
          """
        }
      }
    }

  }

  post {
    changed {
      script {
        notifySlackJobEndNoBranches(branchName: env.BRANCH_NAME,
                                    buildUrl: env.BUILD_URL,
                                    changeUrl: env.CHANGE_URL,
                                    channel: 'jenkins',
                                    buildResult: currentBuild.currentResult)
      }
    }
    cleanup {
      deleteDir()
    }
  }
}
