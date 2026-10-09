plugins {
    kotlin("jvm") version "2.1.20"
    application
}

repositories { mavenCentral() }

dependencies {
    implementation("com.google.code.gson:gson:2.11.0")
    testImplementation(kotlin("test"))
    testImplementation("org.junit.jupiter:junit-jupiter:5.11.4")
}

kotlin { jvmToolchain(17) }

application {
    mainClass.set("ema.MainKt")
}

tasks.test { useJUnitPlatform() }
