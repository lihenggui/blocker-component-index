repositories {
    maven(url = "https://sdk.example.org/releases")
    maven { url = uri("https://other.example.org/android/") }
    maven { setUrl("https://third.example.org/repository") }
}
dependencies {
    implementation("org.example:three:1.0")
    api(group = "org.example", name = "four", version = "4.0")
}
