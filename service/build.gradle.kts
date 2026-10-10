import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("org.springframework.boot") version "3.5.7"
    id("io.spring.dependency-management") version "1.1.7"
    kotlin("jvm") version "2.2.21"
    kotlin("plugin.spring") version "2.2.21"
    id("org.jlleitschuh.gradle.ktlint") version "12.1.2"
}

group = "dev.hybridrag"
version = "0.1.0"

repositories {
    mavenCentral()
}

// Testcontainers 1.21.4 negotiates the API version with Docker Engine 29 (1.21.3 sends a too-old one).
extra["testcontainers.version"] = "1.21.4"

dependencies {
    implementation("org.springframework.boot:spring-boot-starter-web")
    implementation("org.springframework.boot:spring-boot-starter-jdbc")
    implementation("org.springframework.boot:spring-boot-starter-actuator")
    implementation("com.fasterxml.jackson.module:jackson-module-kotlin")
    implementation("org.postgresql:postgresql:42.7.8")
    implementation("com.pgvector:pgvector:0.1.6")
    implementation("io.github.resilience4j:resilience4j-circuitbreaker:2.3.0")
    implementation("io.github.resilience4j:resilience4j-retry:2.3.0")
    implementation("io.opentelemetry:opentelemetry-api:1.54.1")
    implementation("net.logstash.logback:logstash-logback-encoder:8.1")
    implementation("com.github.jsqlparser:jsqlparser:5.3")

    testImplementation("org.springframework.boot:spring-boot-starter-test")
    testImplementation(kotlin("test"))
    testImplementation("org.testcontainers:postgresql")
    testImplementation("org.testcontainers:junit-jupiter")
    testImplementation("org.wiremock:wiremock-standalone:3.13.1")
    testImplementation("org.flywaydb:flyway-core:11.10.5")
    testImplementation("org.flywaydb:flyway-database-postgresql:11.10.5")
    testImplementation("io.opentelemetry:opentelemetry-sdk-testing:1.54.1")
}

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_21)
    }
}

java {
    sourceCompatibility = JavaVersion.VERSION_21
    targetCompatibility = JavaVersion.VERSION_21
}

tasks.withType<Test> {
    useJUnitPlatform()
}

ktlint {
    version.set("1.5.0")
}
