plugins {
    kotlin("jvm") version "2.1.20"
    application
}

repositories { mavenCentral() }

dependencies {
    testImplementation(kotlin("test"))
}

kotlin { jvmToolchain(17) }

application {
    mainClass.set("ema.MainKt")
}

tasks.test { useJUnitPlatform() }
