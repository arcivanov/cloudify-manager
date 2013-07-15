/*******************************************************************************
 * Copyright (c) 2013 GigaSpaces Technologies Ltd. All rights reserved
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *       http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 ******************************************************************************/

package org.cloudifysource.cosmo.statecache;

import com.google.common.collect.HashMultimap;
import com.google.common.collect.ImmutableMap;
import com.google.common.collect.Multimaps;
import com.google.common.collect.SetMultimap;
import com.romix.scala.collection.concurrent.TrieMap;
import org.cloudifysource.cosmo.logging.Logger;
import org.cloudifysource.cosmo.logging.LoggerFactory;

import java.util.Map;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.locks.ReentrantReadWriteLock;

/**
 * The state cache holds the state of each resource, the state of each resource is a collection of properties
 * and their values.
 *
 * @author Eitan Yanovsky
 * @since 0.1
 */
public class StateCache implements AutoCloseable {

    private final TrieMap<StateCacheProperty, String> cache;
    private final SetMultimap<String, StateCacheListener> listeners;
    private final NamedLockProvider lockProvider;
    private final ExecutorService executorService;

    private final Logger logger = LoggerFactory.getLogger(this.getClass());

    public StateCache() {
        this.cache = TrieMap.empty();
        this.lockProvider = new NamedLockProvider();
        this.executorService = Executors.newSingleThreadExecutor();
        this.listeners = Multimaps.synchronizedSetMultimap(HashMultimap.<String, StateCacheListener>create());
    }

    @Override
    public void close() throws Exception {
        executorService.shutdownNow();
    }

    public void put(String resourceId, String property, String value) {
        final ReentrantReadWriteLock.WriteLock writeLock = lockProvider.forName(resourceId).writeLock();
        writeLock.lock();
        try {
            cache.put(new StateCacheProperty(resourceId, property), value);
            final TrieMap<StateCacheProperty, String> snapshot = cache.snapshot();
            final Set<StateCacheListener> resourceListeners = listeners.get(resourceId);
            for (StateCacheListener listener : resourceListeners) {
                submitTriggerEventTask(resourceId, listener, snapshot);
            }
        } finally {
            writeLock.unlock();
        }
    }

    public void subscribe(String resourceId, StateCacheListener listener) {
        final ReentrantReadWriteLock.ReadLock readLock = lockProvider.forName(resourceId).readLock();
        readLock.lock();
        try {
            listeners.put(resourceId, listener);
            final TrieMap<StateCacheProperty, String> snapshot = cache.snapshot();
            for (Map.Entry<StateCacheProperty, String> entry : snapshot.entrySet()) {
                if (entry.getKey().getResourceId().equals(resourceId)) {
                    submitTriggerEventTask(resourceId, listener, snapshot);
                    break;
                }
            }
        } finally {
            readLock.unlock();
        }
    }

    private void submitTriggerEventTask(final String resourceId, final StateCacheListener listener,
                                        final TrieMap<StateCacheProperty, String> snapshot) {
        executorService.submit(new Runnable() {
            @Override
            public void run() {
                boolean remove = true;
                try {
                    remove = listener.onResourceStateChange(new StateCacheSnapshot() {
                        @Override
                        public boolean containsProperty(String resourceId, String property) {
                            return snapshot.containsKey(new StateCacheProperty(resourceId, property));
                        }

                        @Override
                        public String getProperty(String resourceId, String property) {
                            return snapshot.get(new StateCacheProperty(resourceId, property));
                        }

                        @Override
                        public ImmutableMap<String, String> getResourceProperties(String resourceId) {
                            final ImmutableMap.Builder<String, String> builder = ImmutableMap.builder();
                            for (Map.Entry<StateCacheProperty, String> entry : snapshot
                                    .entrySet()) {
                                if (entry.getKey().getResourceId().equals(resourceId))
                                    builder.put(entry.getKey().getResourceId(), entry.getValue());
                            }
                            return builder.build();
                        }
                    });
                } catch (Exception e) {
                    logger.debug("Exception while invoking state change listener, listener will be removed", e);
                } finally {
                    if (remove) {
                        listeners.remove(resourceId, listener);
                    }
                }
            }
        });
    }

    public void removeSubscription(String resourceId, StateCacheListener listener) {
        listeners.remove(resourceId, listener);
    }
}
