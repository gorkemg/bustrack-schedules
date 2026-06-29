# Android Integration — BusTrack Schedules

Kotlin implementation guide for fetching and displaying BusTrack schedule manifests.

## Dependencies (build.gradle.kts)

```kotlin
// Networking
implementation("com.squareup.retrofit2:retrofit:2.11.0")
implementation("com.squareup.retrofit2:converter-moshi:2.11.0")
implementation("com.squareup.moshi:moshi-kotlin:1.15.1")

// Or use Gson instead of Moshi:
// implementation("com.squareup.retrofit2:converter-gson:2.11.0")
```

## Data classes (Moshi)

```kotlin
// data/ScheduleManifest.kt
import com.squareup.moshi.Json
import com.squareup.moshi.JsonClass

@JsonClass(generateAdapter = true)
data class ScheduleManifest(
    @Json(name = "agency_id")      val agencyId: String,
    @Json(name = "schema_version") val schemaVersion: String,
    @Json(name = "generated_at")   val generatedAt: String,  // ISO 8601 string
    @Json(name = "routes")         val routes: Map<String, RouteEntry>,
    @Json(name = "groupings")      val groupings: Map<String, List<RouteGroup>>
)

@JsonClass(generateAdapter = true)
data class RouteEntry(
    @Json(name = "route_short_name") val routeShortName: String,
    @Json(name = "route_long_name")  val routeLongName: String,
    @Json(name = "route_color")      val routeColor: String?,
    @Json(name = "schedule_pdf_url") val schedulePdfUrl: String?,
    @Json(name = "map_pdf_url")      val mapPdfUrl: String?
) {
    /** Display name: long name if non-empty, otherwise short name. */
    val displayName: String get() = routeLongName.ifBlank { routeShortName }

    /** Parses route_color to android.graphics.Color, or null if absent/malformed. */
    val color: Int? get() = routeColor?.let {
        runCatching { android.graphics.Color.parseColor("#$it") }.getOrNull()
    }
}

@JsonClass(generateAdapter = true)
data class RouteGroup(
    @Json(name = "group_id")  val groupId: String,
    @Json(name = "name")      val name: String,
    @Json(name = "route_ids") val routeIds: List<String>
)
```

## Retrofit interface

```kotlin
// network/ScheduleApi.kt
import retrofit2.http.GET
import retrofit2.http.Url

interface ScheduleApi {
    @GET
    suspend fun fetchManifest(@Url url: String): ScheduleManifest
}
```

## Retrofit client setup

```kotlin
// network/ScheduleClient.kt
import com.squareup.moshi.Moshi
import com.squareup.moshi.kotlin.reflect.KotlinJsonAdapterFactory
import retrofit2.Retrofit
import retrofit2.converter.moshi.MoshiConverterFactory

object ScheduleClient {
    // Dummy base URL — all calls use @Url and override this.
    private const val BASE = "https://github.com/"

    val api: ScheduleApi by lazy {
        val moshi = Moshi.Builder()
            .addLast(KotlinJsonAdapterFactory())
            .build()

        Retrofit.Builder()
            .baseUrl(BASE)
            .addConverterFactory(MoshiConverterFactory.create(moshi))
            .build()
            .create(ScheduleApi::class.java)
    }
}

object ScheduleEndpoints {
    const val UTA_PRODUCTION  = "https://github.com/gorkemg/bustrack-schedules/releases/download/uta/uta_schedules.json"
    const val PVTA_PRODUCTION = "https://github.com/gorkemg/bustrack-schedules/releases/download/pvta/pvta_schedules.json"
    const val UTA_BETA        = "https://github.com/gorkemg/bustrack-schedules/releases/download/uta-beta/uta_schedules.json"
    const val PVTA_BETA       = "https://github.com/gorkemg/bustrack-schedules/releases/download/pvta-beta/pvta_schedules.json"
}
```

## Repository with disk cache

```kotlin
// repository/ScheduleRepository.kt
import android.content.Context
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.time.Instant
import java.time.temporal.ChronoUnit

class ScheduleRepository(private val context: Context) {
    private val cacheDir = File(context.cacheDir, "bustrack_schedules").also { it.mkdirs() }
    private val api = ScheduleClient.api

    suspend fun getManifest(agency: String, url: String, forceRefresh: Boolean = false): Result<ScheduleManifest> {
        val cacheFile = File(cacheDir, "${agency.lowercase()}_schedules.json")

        // Validate schema version before returning any manifest.
        fun validate(manifest: ScheduleManifest): Result<ScheduleManifest> {
            return if (manifest.schemaVersion.startsWith("1")) {
                Result.success(manifest)
            } else {
                Result.failure(IllegalStateException("Unsupported schema version: ${manifest.schemaVersion}"))
            }
        }

        fun isStale(): Boolean {
            if (!cacheFile.exists()) return true
            val age = ChronoUnit.HOURS.between(
                Instant.ofEpochMilli(cacheFile.lastModified()), Instant.now()
            )
            return age > 7 * 24
        }

        return withContext(Dispatchers.IO) {
            // Attempt network refresh if stale or forced.
            if (forceRefresh || isStale()) {
                runCatching { api.fetchManifest(url) }
                    .onSuccess { manifest ->
                        // Persist raw JSON is not straightforward with Retrofit — 
                        // use OkHttp interceptor or a secondary OkHttpClient.data call for byte caching.
                        // Simplification: write the object back via Moshi for offline use.
                        cacheFile.writeText(serializeManifest(manifest))
                        return@withContext validate(manifest)
                    }
                    .onFailure { /* fall through to cache */ }
            }

            // Serve from cache.
            if (cacheFile.exists()) {
                runCatching { deserializeManifest(cacheFile.readText()) }
                    .fold({ validate(it) }, { Result.failure(it) })
            } else {
                Result.failure(Exception("No cached data available and network request failed."))
            }
        }
    }

    private fun serializeManifest(manifest: ScheduleManifest): String {
        val moshi = com.squareup.moshi.Moshi.Builder()
            .addLast(com.squareup.moshi.kotlin.reflect.KotlinJsonAdapterFactory()).build()
        return moshi.adapter(ScheduleManifest::class.java).toJson(manifest)
    }

    private fun deserializeManifest(json: String): ScheduleManifest {
        val moshi = com.squareup.moshi.Moshi.Builder()
            .addLast(com.squareup.moshi.kotlin.reflect.KotlinJsonAdapterFactory()).build()
        return moshi.adapter(ScheduleManifest::class.java).fromJson(json)!!
    }
}
```

## ViewModel

```kotlin
// viewmodel/ScheduleViewModel.kt
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

data class ScheduleUiState(
    val groups: List<RouteGroup> = emptyList(),
    val routes: Map<String, RouteEntry> = emptyMap(),
    val isLoading: Boolean = false,
    val error: String? = null
)

class ScheduleViewModel(
    private val repository: ScheduleRepository,
    private val agency: String,
    private val url: String
) : ViewModel() {

    private val _uiState = MutableStateFlow(ScheduleUiState(isLoading = true))
    val uiState: StateFlow<ScheduleUiState> = _uiState

    fun load(forceRefresh: Boolean = false) {
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isLoading = true, error = null)
            repository.getManifest(agency, url, forceRefresh)
                .onSuccess { manifest ->
                    // groupings has one key ("vehicle_type" or "region"); take its value.
                    val groups = manifest.groupings.values.firstOrNull() ?: emptyList()
                    _uiState.value = ScheduleUiState(
                        groups = groups,
                        routes = manifest.routes,
                        isLoading = false
                    )
                }
                .onFailure { error ->
                    _uiState.value = _uiState.value.copy(
                        isLoading = false,
                        error = error.message
                    )
                }
        }
    }
}
```

## Opening PDFs

```kotlin
// In Fragment or Activity:
fun openPdf(url: String) {
    val intent = Intent(Intent.ACTION_VIEW, Uri.parse(url))
    intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    // Let the system choose a PDF viewer or browser.
    startActivity(Intent.createChooser(intent, "Open with"))
}

// Guard before calling — never pass a null URL:
entry.schedulePdfUrl?.let { openPdf(it) }
```

## route_color usage in Compose

```kotlin
// In a Composable:
@Composable
fun RouteColorBadge(entry: RouteEntry) {
    val backgroundColor = entry.color
        ?.let { Color(it) }
        ?: MaterialTheme.colorScheme.primary

    Box(
        modifier = Modifier
            .background(backgroundColor, shape = RoundedCornerShape(6.dp))
            .padding(horizontal = 8.dp, vertical = 4.dp)
    ) {
        Text(
            text = entry.routeShortName,
            color = Color.White,
            style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold)
        )
    }
}
```
